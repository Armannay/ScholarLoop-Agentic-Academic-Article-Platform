"""Generic agent runtime: validation, the single repair, accounting and hot-swap.

Every test drives ``MockProvider``; no network, no keys. The provider wrapper records
the *requests*, which is how the repair tests prove what was (and was not) re-sent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.agents.base import (
    REPAIR_SYSTEM_PROMPT,
    Agent,
    AgentProvider,
    BindingResolver,
    agent_output_schema,
)
from app.agents.binding import CallableBindingResolver, UserBindingResolver
from app.agents.context import AgentRunState, DefaultContextBuilder
from app.agents.errors import AgentOutputInvalid, AgentTaskMismatch
from app.agents.governor import CallBudgetGovernor
from app.agents.pool import PoolAgentProvider
from app.contracts.binding import AgentBinding
from app.contracts.enums import (
    AgentRole,
    BindingRole,
    BindingStrategy,
    SectionStatus,
)
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.section import SectionState
from app.contracts.task import Task
from app.core.errors import BudgetExceeded
from app.providers.errors import NoAccountAvailable, ProviderTimeout
from app.providers.mock import MockProvider, Script
from app.skills.loader import SkillLoader
from tests.factories import make_request, sample_ulid

pytestmark = pytest.mark.agents


# --------------------------------------------------------------------------- fakes
class RecordingProvider:
    """Delegates to MockProvider and keeps the exact requests that were sent."""

    def __init__(self, inner: MockProvider) -> None:
        self._inner = inner
        self.requests: list[LLMRequest] = []
        self.models: list[str | None] = []
        self.roles: list[str | None] = []

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
    ) -> LLMResponse:
        self.requests.append(request)
        self.models.append(model)
        self.roles.append(role)
        return await self._inner.generate(request, model=model, role=role)

    @property
    def call_count(self) -> int:
        return len(self.requests)


class StubWriter(Agent):
    """The runtime under test. A concrete role agent belongs to a later step."""

    role = AgentRole.WRITER


# --------------------------------------------------------------------------- helpers
def make_task(
    *,
    role: AgentRole = AgentRole.WRITER,
    section_ids: tuple[str, ...] = ("introduction",),
    token_budget: int = 50_000,
    title: str = "Draft the introduction",
    instructions: str = "Write the introduction from the brief and the provided data.",
    acceptance: tuple[str, ...] = ("States the gap and the contribution",),
    task_id: str | None = None,
) -> Task:
    return Task(
        id=task_id or sample_ulid(1),
        title=title,
        role=role,
        section_ids=list(section_ids),
        source_ids=[sample_ulid(5)],
        instructions=instructions,
        acceptance=list(acceptance),
        token_budget=token_budget,
    )


def make_binding(
    *,
    model: str = "mock-a",
    max_output_tokens: int = 512,
    max_input_tokens: int = 8_000,
    role: BindingRole = BindingRole.WRITER,
    enabled: bool = True,
) -> AgentBinding:
    return AgentBinding(
        role=role,
        account_ids=[sample_ulid(7)],
        strategy=BindingStrategy.DEDICATED,
        model=model,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        enabled=enabled,
    )


def make_section(section_id: str = "introduction", *, version: int = 2) -> SectionState:
    return SectionState(
        id=section_id,
        title="Introduction",
        order=0,
        version=version,
        hash="sha256:" + "ab" * 32,
        words=120,
        status=SectionStatus.DRAFT,
    )


def make_state(**kwargs: Any) -> AgentRunState:
    defaults: dict[str, Any] = {
        "iter": 3,
        "sections": (make_section(),),
    }
    defaults.update(kwargs)
    return AgentRunState(**defaults)


def result_payload(
    task: Task,
    *,
    section_id: str = "introduction",
    base_version: int = 2,
    summary: str = "Drafted the introduction from the brief.",
) -> dict[str, Any]:
    """A minimal valid AgentResult: runtime-owned fields are deliberately absent."""
    return {
        "entry": {
            "task_id": task.id,
            "role": task.role.value,
            "targets": [section_id],
            "summary": summary,
        },
        "patches": [
            {
                "section_id": section_id,
                "base_version": base_version,
                "ops": [
                    {
                        "op": "replace_section",
                        "text": "Agentic writing loops trade tokens for quality.",
                    }
                ],
            }
        ],
    }


def make_builder(
    repo_root: Path,
    *,
    rules: tuple[str, ...] = ("ROOT RULES TEXT: never fabricate.", "WRITER RULES TEXT."),
) -> DefaultContextBuilder:
    return DefaultContextBuilder(skills=SkillLoader(repo_root / "skills"), rules=rules)


def make_agent(
    provider: AgentProvider,
    resolver: BindingResolver,
    builder: DefaultContextBuilder,
    *,
    governor: CallBudgetGovernor | None = None,
    id_factory: Callable[[], str] | None = None,
) -> StubWriter:
    kwargs: dict[str, Any] = {}
    if id_factory is not None:
        kwargs["id_factory"] = id_factory
    return StubWriter(
        provider=provider,
        bindings=resolver,
        governor=governor or CallBudgetGovernor(),
        context_builder=builder,
        **kwargs,
    )


# --------------------------------------------------------------------------- tests
async def test_valid_output_returns_the_expected_agent_result(repo_root: Path) -> None:
    """One valid answer -> a contract-valid result with measured accounting."""
    task = make_task()
    inner = MockProvider(Script().json_reply("writer", 0, result_payload(task)))
    provider = RecordingProvider(inner)
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    result = await agent.run(task, make_state())

    assert result.entry.role is AgentRole.WRITER
    assert result.entry.task_id == task.id
    assert result.entry.iter == 3
    assert result.entry.summary.startswith("Drafted")
    assert result.entry.patch_ref is not None
    assert result.entry.patch_ref.startswith("patch:")
    assert len(result.patches) == 1
    assert result.patches[0].patch_id is not None
    assert result.patches[0].section_id == "introduction"
    assert result.entry.tokens.total == inner.total_tokens().total
    assert result.entry.tokens.total > 0
    assert provider.call_count == 1
    assert provider.roles == ["writer"]
    assert provider.models == ["mock-a"]


async def test_successful_output_causes_no_repair_call(repo_root: Path) -> None:
    """A first-try pass sends exactly one request."""
    task = make_task()
    provider = RecordingProvider(
        MockProvider(Script().json_reply("writer", 0, result_payload(task)))
    )
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    await agent.run(task, make_state())

    assert provider.call_count == 1
    assert all(block.text != REPAIR_SYSTEM_PROMPT for block in provider.requests[0].system_blocks)


async def test_schema_validation_failure_triggers_exactly_one_repair(
    repo_root: Path,
) -> None:
    """Invalid contract shape -> one repair with the good answer."""
    task = make_task()
    script = (
        Script()
        .json_reply("writer", 0, {"entry": {"role": "writer"}})
        .json_reply("writer", 1, result_payload(task))
    )
    inner = MockProvider(script)
    provider = RecordingProvider(inner)
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    result = await agent.run(task, make_state())

    assert provider.call_count == 2
    assert inner.call_count == 2
    assert result.entry.task_id == task.id
    assert result.entry.tokens.total == inner.total_tokens().total
    assert provider.requests[1].system_blocks[0].text == REPAIR_SYSTEM_PROMPT


async def test_patch_validation_failure_triggers_exactly_one_repair(
    repo_root: Path,
) -> None:
    """A patch outside the task's sections is a validation failure, not a crash."""
    task = make_task(section_ids=("introduction",))
    outside = result_payload(task, section_id="conclusion")
    script = Script().json_reply("writer", 0, outside).json_reply("writer", 1, result_payload(task))
    provider = RecordingProvider(MockProvider(script))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    result = await agent.run(task, make_state())

    assert provider.call_count == 2
    repair_text = provider.requests[1].messages[0].text
    assert "patches[0].section_id" in repair_text
    assert "not in task.section_ids" in repair_text
    assert result.patches[0].section_id == "introduction"


async def test_repair_receives_only_errors_and_previous_output(repo_root: Path) -> None:
    """The repair prompt must not re-send rules, skills, task text or brief."""
    task = make_task()
    invalid = '{"entry": {"role": "writer"}}'
    script = Script().reply("writer", 0, invalid).json_reply("writer", 1, result_payload(task))
    provider = RecordingProvider(MockProvider(script))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    await agent.run(task, make_state())

    repair = provider.requests[1]
    assert len(repair.system_blocks) == 1
    assert repair.system_blocks[0].text == REPAIR_SYSTEM_PROMPT
    assert len(repair.messages) == 1
    body = repair.messages[0].text
    assert invalid in body
    assert "Validation errors:" in body
    assert "task_id" in body
    for forbidden in (
        "ROOT RULES TEXT",
        "WRITER RULES TEXT",
        "Complete the task below",
        task.instructions,
        "Skill index",
        "sources",
        "brief",
    ):
        assert forbidden not in body, f"repair request leaked {forbidden!r}"


async def test_invalid_output_after_repair_fails_cleanly_without_another_repair(
    repo_root: Path,
) -> None:
    """Exactly one repair: the second failure raises and spends no third call."""
    task = make_task()
    script = (
        Script()
        .json_reply("writer", 0, {"entry": {"role": "writer"}})
        .json_reply("writer", 1, {"entry": {"role": "writer"}})
        .json_reply("writer", 2, result_payload(task))
    )
    inner = MockProvider(script)
    provider = RecordingProvider(inner)
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    with pytest.raises(AgentOutputInvalid) as caught:
        await agent.run(task, make_state())

    assert caught.value.attempts == 2
    assert caught.value.role == "writer"
    assert caught.value.task_id == task.id
    assert caught.value.errors
    assert caught.value.tokens.total == inner.total_tokens().total
    assert provider.call_count == 2
    assert getattr(caught.value, "http_status", None) == 502


async def test_stable_prompt_prefix_is_identical_across_equivalent_calls(
    repo_root: Path,
) -> None:
    """Same rules, same skill index, same selection => same prefix, byte for byte."""
    task = make_task()
    other_task = make_task(task_id=sample_ulid(9))  # same text, different id
    state = make_state()
    builder = make_builder(repo_root)

    first = builder.build(task=task, run_state=state)
    second = builder.build(task=task, run_state=state)
    third = builder.build(task=other_task, run_state=state)
    assert first.prefix_hash == second.prefix_hash == third.prefix_hash
    assert first.system_blocks == second.system_blocks == third.system_blocks
    assert first.messages != third.messages  # the task id lives in the message, not the prefix

    provider = RecordingProvider(
        MockProvider(
            Script()
            .json_reply("writer", 0, result_payload(task))
            .json_reply("writer", 1, result_payload(task))
        )
    )
    agent = make_agent(provider, CallableBindingResolver(lambda role: make_binding()), builder)
    await agent.run(task, state)
    await agent.run(task, state)
    assert provider.requests[0].system_blocks == provider.requests[1].system_blocks


async def test_binding_is_resolved_at_call_time_and_follows_a_hot_swap(
    repo_root: Path,
) -> None:
    """Swap the binding between two runs: the second call uses the new model and cap."""
    task = make_task()
    holder: dict[str, AgentBinding] = {
        "binding": make_binding(model="mock-a", max_output_tokens=256)
    }
    resolver = CallableBindingResolver(lambda role: holder["binding"])
    provider = RecordingProvider(
        MockProvider(
            Script()
            .json_reply("writer", 0, result_payload(task))
            .json_reply("writer", 1, result_payload(task))
        )
    )
    agent = make_agent(provider, resolver, make_builder(repo_root))

    await agent.run(task, make_state())
    assert provider.models == ["mock-a"]
    assert provider.requests[0].max_output_tokens == 256

    holder["binding"] = make_binding(model="mock-b", max_output_tokens=1024)
    await agent.run(task, make_state())

    assert provider.models == ["mock-a", "mock-b"]
    assert provider.requests[1].max_output_tokens == 1024


async def test_missing_or_disabled_binding_fails_before_any_call(
    repo_root: Path,
) -> None:
    """No active binding is a user condition, not a validation failure."""
    task = make_task()
    provider = RecordingProvider(MockProvider(Script()))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: None), make_builder(repo_root)
    )

    with pytest.raises(NoAccountAvailable):
        await agent.run(task, make_state())
    assert provider.call_count == 0

    disabled = make_binding(enabled=False)
    agent = make_agent(
        provider,
        CallableBindingResolver(lambda role: disabled),
        make_builder(repo_root),
    )
    with pytest.raises(NoAccountAvailable):
        await agent.run(task, make_state())
    assert provider.call_count == 0


async def test_provider_failure_is_not_treated_as_a_validation_failure(
    repo_root: Path,
) -> None:
    """A timeout propagates immediately; no repair call is attempted."""
    task = make_task()
    script = Script().fail("writer", 0, "timeout").json_reply("writer", 1, result_payload(task))
    provider = RecordingProvider(MockProvider(script))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    with pytest.raises(ProviderTimeout):
        await agent.run(task, make_state())
    assert provider.call_count == 1


async def test_task_owned_by_another_role_is_refused(repo_root: Path) -> None:
    """An agent may not execute another role's task."""
    task = make_task(role=AgentRole.REVIEWER)
    provider = RecordingProvider(MockProvider(Script()))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    with pytest.raises(AgentTaskMismatch):
        await agent.run(task, make_state())
    assert provider.call_count == 0


async def test_governor_refusal_happens_before_the_provider_call(
    repo_root: Path,
) -> None:
    """The task budget bounds a single call; an impossible call is never sent."""
    task = make_task(token_budget=100)
    provider = RecordingProvider(MockProvider(Script()))
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    with pytest.raises(BudgetExceeded):
        await agent.run(task, make_state())
    assert provider.call_count == 0


async def test_binding_input_cap_is_enforced_locally(repo_root: Path) -> None:
    """A prompt larger than the binding's max_input_tokens is refused, not truncated."""
    task = make_task()
    binding = make_binding(max_input_tokens=1_000, max_output_tokens=512)
    provider = RecordingProvider(MockProvider(Script()))
    big_rules = ("x" * 8_000,)
    agent = make_agent(
        provider,
        CallableBindingResolver(lambda role: binding),
        make_builder(repo_root, rules=big_rules),
    )

    with pytest.raises(BudgetExceeded):
        await agent.run(task, make_state())
    assert provider.call_count == 0


def test_output_schema_does_not_ask_the_model_to_compute_accounting() -> None:
    """Runtime-owned fields stay optional in the constrained output schema."""
    schema = agent_output_schema()
    required = schema["$defs"]["ChangeEntry"]["required"]
    assert "tokens" not in required
    assert "iter" not in required
    assert "summary" in required
    assert "entry" in schema["required"]


async def test_model_claimed_accounting_is_replaced_by_measured_usage(
    repo_root: Path,
) -> None:
    """The ledger records what was spent, never what the model says it spent."""
    task = make_task()
    payload = result_payload(task)
    payload["entry"]["iter"] = 999
    payload["entry"]["tokens"] = {"in": 1, "out": 1, "cached": 0}
    inner = MockProvider(Script().json_reply("writer", 0, payload))
    provider = RecordingProvider(inner)
    agent = make_agent(
        provider, CallableBindingResolver(lambda role: make_binding()), make_builder(repo_root)
    )

    result = await agent.run(task, make_state())

    assert result.entry.iter == 3
    assert result.entry.tokens.total == inner.total_tokens().total
    assert result.entry.tokens.total != 2
    assert result.patches[0].patch_id is not None
    assert result.entry.patch_ref == f"patch:{result.patches[0].patch_id}"


async def test_user_binding_resolver_reads_the_store_on_every_call(
    repo_root: Path,
) -> None:
    """The real integration seam: the account store is consulted per run, not cached."""
    task = make_task()

    class Store:
        def __init__(self) -> None:
            self.binding = make_binding(model="mock-a")
            self.calls: list[tuple[str, str]] = []

        def get_binding(self, user_id: str, role: str) -> AgentBinding:
            self.calls.append((user_id, role))
            return self.binding

    store = Store()
    resolver = UserBindingResolver(store, user_id="user-1")
    provider = RecordingProvider(
        MockProvider(
            Script()
            .json_reply("writer", 0, result_payload(task))
            .json_reply("writer", 1, result_payload(task))
        )
    )
    agent = make_agent(provider, resolver, make_builder(repo_root))

    await agent.run(task, make_state())
    store.binding = make_binding(model="mock-b")
    await agent.run(task, make_state())

    assert store.calls == [("user-1", "writer"), ("user-1", "writer")]
    assert provider.models == ["mock-a", "mock-b"]


async def test_pool_adapter_supplies_the_owner_to_the_pool() -> None:
    """Adapter glue only: the pool still owns routing, failover and billing."""

    class FakePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        async def generate(
            self,
            request: LLMRequest,
            *,
            role: str,
            user_id: str,
            model: str | None = None,
        ) -> LLMResponse:
            self.calls.append((role, user_id, model))
            return LLMResponse(
                text="ok",
                usage=TokenUsage(**{"in": 1, "out": 1, "cached": 0}),
                model=model or "pool-default",
                latency_ms=1,
            )

    pool = FakePool()
    adapter = PoolAgentProvider(pool, user_id="user-1")

    response = await adapter.generate(make_request(), model="m1", role="writer")

    assert response.text == "ok"
    assert pool.calls == [("writer", "user-1", "m1")]
    with pytest.raises(ValueError):
        await adapter.generate(make_request())


def test_mock_provider_satisfies_the_agent_provider_protocol() -> None:
    """The runtime's provider seam is the established abstraction, not a lookalike."""
    assert isinstance(MockProvider(), AgentProvider)
