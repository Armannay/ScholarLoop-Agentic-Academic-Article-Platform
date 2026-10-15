"""Step 5 runtime, skills and scoped-tool tests; providers remain MockProvider-only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.agents.base import Agent
from app.agents.registry import (
    AgentDisabledError,
    AgentRegistry,
    UnknownAgentRoleError,
)
from app.contracts.binding import AgentBinding
from app.contracts.enums import AgentRole, BindingRole, BindingStrategy, ChangeStatus
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.task import Task
from app.providers.mock import MockProvider
from app.scripts.lint_skills import lint_skills
from app.skills.loader import SkillLoader, SkillPolicy
from app.tools.base import (
    ToolDisabledError,
    ToolNotPermittedError,
    ToolRegistry,
    ToolScope,
    ToolScopeError,
)
from tests.factories import sample_payloads, sample_ulid


class MutableBindingResolver:
    """Return the latest binding and count every call-time resolution."""

    def __init__(self, binding: AgentBinding) -> None:
        self.binding = binding
        self.calls: list[str] = []

    def resolve_binding(self, role: str) -> AgentBinding:
        self.calls.append(role)
        return self.binding


class RecordingMockProvider(MockProvider):
    """MockProvider that retains request objects for safe prompt-shape assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LLMRequest] = []

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))
        return await super().generate(
            request,
            model=model,
            role=role,
            account_id=account_id,
        )


class MockProviderPool:
    """Provider pool test double backed only by MockProvider."""

    def __init__(self, provider: RecordingMockProvider, binding: AgentBinding) -> None:
        self.provider = provider
        self.binding = binding
        self.provider_lookups: list[AgentBinding] = []

    def resolve_binding(self, role: str) -> AgentBinding:
        assert role == self.binding.role.value
        return self.binding

    def get_provider(self, binding: AgentBinding) -> RecordingMockProvider:
        self.provider_lookups.append(binding)
        return self.provider


class StaticContextBuilder:
    """Build a stable cached prefix plus task-specific user message."""

    def __init__(self) -> None:
        self.calls = 0

    def build(self, task: Task, run_state: object) -> LLMRequest:
        self.calls += 1
        return LLMRequest(
            system_blocks=[{"text": "stable root rules and skill index", "cache": True}],
            messages=[{"role": "user", "text": f"task={task.id}; context=ORIGINAL_CONTEXT"}],
            max_output_tokens=128,
        )


class RecordingGovernor:
    """Governor test double that records each prechecked structured request."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def precheck(self, request: LLMRequest, binding: AgentBinding, run_state: object) -> None:
        self.requests.append(request.model_copy(deep=True))


def make_binding(*, max_output_tokens: int = 777) -> AgentBinding:
    return AgentBinding(
        role=BindingRole.WRITER,
        account_ids=[sample_ulid(30)],
        strategy=BindingStrategy.DEDICATED,
        model="mock-writer-model",
        max_input_tokens=24_000,
        max_output_tokens=max_output_tokens,
    )


def make_task(**updates: Any) -> Task:
    payload = dict(sample_payloads()["Task"])
    payload.update(updates)
    return Task.model_validate(payload)


def valid_agent_output(*, summary: str = "Applied one scoped patch.") -> dict[str, Any]:
    return {
        "summary": summary,
        "patches": [
            {
                "section_id": "introduction",
                "base_version": 0,
                "ops": [{"op": "replace_section", "text": "A concise supported draft."}],
            }
        ],
        "source_cards": [],
        "sections": [],
        "report": None,
        "status": "ok",
        "issues_opened": [],
        "issues_closed": [],
    }


def make_agent(
    provider: RecordingMockProvider,
    resolver: MutableBindingResolver,
    *,
    registry: AgentRegistry | None = None,
    binding: AgentBinding | None = None,
) -> tuple[Agent, RecordingGovernor, StaticContextBuilder, MockProviderPool]:
    selected_binding = binding if binding is not None else make_binding()
    pool = MockProviderPool(provider, selected_binding)
    governor = RecordingGovernor()
    context_builder = StaticContextBuilder()
    agent = Agent(
        context_builder,
        governor,
        pool,
        binding_resolver=registry if registry is not None else resolver,
        role=AgentRole.WRITER,
    )
    return agent, governor, context_builder, pool


async def test_agent_runs_context_governor_provider_and_builds_contract_result() -> None:
    provider = RecordingMockProvider()
    provider.script_reply("writer", 0, json.dumps(valid_agent_output()))
    binding = make_binding(max_output_tokens=513)
    resolver = MutableBindingResolver(binding)
    agent, governor, context_builder, pool = make_agent(provider, resolver, binding=binding)

    result = await agent.run(make_task(), {"iter": 4})

    assert result.entry.status is ChangeStatus.OK
    assert result.entry.iter == 4
    assert result.entry.role is AgentRole.WRITER
    assert result.entry.task_id == make_task().id
    assert len(result.patches) == 1
    assert result.patches[0].patch_id is not None
    assert result.entry.patch_ref == f"patch:{result.patches[0].patch_id}"
    assert result.entry.tokens == provider.total_tokens()
    assert provider.requests[0].max_output_tokens == 513
    assert provider.requests[0].json_schema is not None
    assert provider.requests[0].json_schema["type"] == "object"
    assert len(governor.requests) == 1
    assert context_builder.calls == 1
    assert resolver.calls == ["writer"]
    assert len(pool.provider_lookups) == 1


async def test_invalid_output_gets_exactly_one_context_free_repair_then_clean_failure() -> None:
    provider = RecordingMockProvider()
    provider.script_reply("writer", 0, "not-json")
    provider.script_reply("writer", 1, json.dumps({"unexpected": "still invalid"}))
    binding = make_binding()
    resolver = MutableBindingResolver(binding)
    agent, governor, _, pool = make_agent(provider, resolver, binding=binding)

    result = await agent.run(make_task(), {"iter": 2})

    assert provider.call_count == 2
    assert len(provider.requests) == 2
    assert len(governor.requests) == 2
    assert len(pool.provider_lookups) == 2
    assert resolver.calls == ["writer", "writer"]
    assert result.entry.status is ChangeStatus.FAILED
    assert result.patches == []
    assert result.entry.patch_ref is None
    assert result.entry.tokens == provider.total_tokens()

    repair_request = provider.requests[1]
    assert len(repair_request.messages) == 1
    repair_payload = json.loads(repair_request.messages[0].text)
    assert set(repair_payload) == {"validation_error", "previous_output"}
    assert repair_payload["previous_output"] == "not-json"
    assert "ORIGINAL_CONTEXT" not in repair_request.messages[0].text
    assert "ORIGINAL_CONTEXT" not in "\n".join(block.text for block in repair_request.system_blocks)
    assert repair_request.json_schema is not None
    assert repair_request.json_schema["type"] == "object"


async def test_patch_scope_validation_uses_the_single_repair_call() -> None:
    provider = RecordingMockProvider()
    invalid = valid_agent_output()
    invalid["patches"][0]["section_id"] = "conclusion"
    provider.script_reply("writer", 0, json.dumps(invalid))
    provider.script_reply("writer", 1, json.dumps(valid_agent_output()))
    binding = make_binding()
    resolver = MutableBindingResolver(binding)
    agent, _, _, _ = make_agent(provider, resolver, binding=binding)

    result = await agent.run(make_task(), {"iter": 5})

    assert provider.call_count == 2
    assert result.entry.status is ChangeStatus.OK
    repair_payload = json.loads(provider.requests[1].messages[0].text)
    assert repair_payload["previous_output"] == json.dumps(invalid)
    assert "outside task.section_ids" in repair_payload["validation_error"]


async def test_successful_repair_returns_valid_patches_and_combines_usage() -> None:
    provider = RecordingMockProvider()
    provider.script_reply("writer", 0, json.dumps({"wrong": "shape"}))
    provider.script_reply("writer", 1, json.dumps(valid_agent_output(summary="Repaired output.")))
    binding = make_binding(max_output_tokens=901)
    resolver = MutableBindingResolver(binding)
    agent, governor, _, _ = make_agent(provider, resolver, binding=binding)

    result = await agent.run(make_task(), {"iteration": 7})

    assert provider.call_count == 2
    assert len(governor.requests) == 2
    assert result.entry.status is ChangeStatus.OK
    assert result.entry.iter == 7
    assert result.entry.summary == "Repaired output."
    assert result.entry.tokens == provider.total_tokens()
    assert all(request.max_output_tokens == 901 for request in provider.requests)


async def test_context_stable_prefix_survives_repeated_agent_calls() -> None:
    provider = RecordingMockProvider()
    reply = json.dumps(
        {
            "summary": "No change was needed.",
            "patches": [],
            "source_cards": [],
            "sections": [],
            "report": None,
            "status": "ok",
            "issues_opened": [],
            "issues_closed": [],
        }
    )
    provider.script_reply("writer", 0, reply)
    provider.script_reply("writer", 1, reply)
    binding = make_binding()
    resolver = MutableBindingResolver(binding)
    agent, _, _, _ = make_agent(provider, resolver, binding=binding)

    await agent.run(make_task(), {"iter": 1})
    await agent.run(make_task(), {"iter": 2})

    prefixes = [request.system_blocks[0].text for request in provider.requests]
    assert prefixes == ["stable root rules and skill index"] * 2
    assert [request.system_blocks[0].cache for request in provider.requests] == [True, True]


async def test_registry_enable_disable_unknown_role_and_call_time_binding_resolution() -> None:
    binding = make_binding()
    resolver = MutableBindingResolver(binding)
    registry = AgentRegistry(
        {AgentRole.WRITER: Agent},
        binding_resolver=resolver,
    )

    assert registry.resolve("writer") is Agent
    with pytest.raises(UnknownAgentRoleError, match="unknown agent role"):
        registry.resolve("planner")
    with pytest.raises(UnknownAgentRoleError, match="no agent class"):
        registry.resolve("researcher")

    registry.disable("writer")
    with pytest.raises(AgentDisabledError, match="disabled"):
        registry.resolve("writer")
    registry.enable("writer")

    provider = RecordingMockProvider()
    provider.script_reply("writer", 0, json.dumps(valid_agent_output()))
    context = StaticContextBuilder()
    governor = RecordingGovernor()
    pool = MockProviderPool(provider, binding)
    result = await registry.run(
        make_task(),
        {"iter": 3},
        context_builder=context,
        governor=governor,
        provider_pool=pool,
    )
    assert result.entry.status is ChangeStatus.OK
    assert resolver.calls == ["writer"]


def test_skill_index_contains_only_stable_names_and_descriptions(repo_root: Path) -> None:
    loader = SkillLoader(repo_root / "skills")

    first = loader.index()
    second = loader.index()

    assert loader._loaded == {}
    assert first == second
    assert all(set(item) == {"name", "description"} for item in first)
    assert loader.render_index() == loader.render_index()
    assert [item["name"] for item in first] == sorted(item["name"] for item in first)


def test_skill_selection_is_deterministic_role_scoped_and_token_bounded(repo_root: Path) -> None:
    loader = SkillLoader(repo_root / "skills", policy=SkillPolicy(max_skills=2, max_tokens=500))
    task = make_task(
        title="Prepare an abstract and literature review",
        instructions="Use APA7 citations and a minimal patch for the section update.",
        token_budget=900,
    )

    first = loader.select(task)
    second = loader.select(task)

    assert [skill.name for skill in first] == [skill.name for skill in second]
    assert len(first) <= 2
    assert sum(skill.token_count for skill in first) <= 500
    assert all(AgentRole.WRITER in skill.roles for skill in first)


def test_skill_linter_passes_starters_and_rejects_declared_budget_overrun(
    repo_root: Path, tmp_path: Path
) -> None:
    assert lint_skills(repo_root / "skills") == []

    over_budget = tmp_path / "over-budget"
    over_budget.mkdir()
    (over_budget / "SKILL.md").write_text(
        "---\n"
        "name: over-budget\n"
        "description: A tiny linter fixture.\n"
        "roles: [writer]\n"
        "max_tokens: 1\n"
        "triggers: [lint]\n"
        "---\n"
        "This body intentionally contains more than one token.\n",
        encoding="utf-8",
    )
    errors = lint_skills(tmp_path)
    assert len(errors) == 1
    assert "exceeding declared max_tokens 1" in errors[0]


class EchoTool:
    name = "read_source"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"source_id": {"type": "string"}},
        "required": ["source_id"],
        "additionalProperties": False,
    }
    token_cost_hint = 12

    def __init__(self) -> None:
        self.calls = 0

    def run(self, arguments: Mapping[str, Any]) -> object:
        self.calls += 1
        return {"body": "x" * 200, "source_id": arguments.get("source_id")}


async def test_tool_project_scope_role_permissions_untrusted_wrapper_and_size_cap() -> None:
    registry = ToolRegistry(
        allowed_tools_by_role={AgentRole.RESEARCHER: {"read_source"}},
        max_result_bytes=48,
    )
    tool = EchoTool()
    registry.register(tool)

    assert [item["name"] for item in registry.schemas_for("researcher")] == ["read_source"]
    assert registry.schemas_for("writer") == []
    researcher_access = registry.for_role("researcher", project_id="project-a")
    assert [item["name"] for item in researcher_access.schemas] == ["read_source"]

    result = await researcher_access.run("read_source", {"source_id": sample_ulid(10)})
    assert result.untrusted is True
    assert result.truncated is True
    assert len(result.data.encode("utf-8")) <= 48
    assert result.token_cost_hint == 12
    assert "UNTRUSTED TOOL DATA" in result.as_prompt_text()
    assert tool.calls == 1

    writer_access = registry.for_role("writer")
    assert not hasattr(writer_access, "registry")
    assert not hasattr(writer_access, "role")
    assert not hasattr(writer_access, "project_id")
    with pytest.raises(TypeError):
        await writer_access.run(
            "read_source",
            {"source_id": sample_ulid(10)},
            role="researcher",
        )
    with pytest.raises(ToolNotPermittedError, match="not permitted"):
        await writer_access.run("read_source", {"source_id": sample_ulid(10)})
    assert tool.calls == 1

    registry.disable_for_project("project-a", "read_source")
    assert researcher_access.schemas == []
    with pytest.raises(ToolDisabledError, match="disabled"):
        await researcher_access.run("read_source", {"source_id": sample_ulid(10)})
    assert tool.calls == 1
    registry.enable_for_project("project-a", "read_source")
    assert researcher_access.schemas

    with pytest.raises(ToolScopeError, match="only configure root-registered"):
        registry.enable_for_project("project-a", "project-only-tool")
    with pytest.raises(ToolScopeError, match="only enable or disable"):
        registry.register(tool, scope=ToolScope.PROJECT)
