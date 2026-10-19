"""LLMPlanner tests - MockProvider only (HARD RULE 5).

Covers the guardrails (Anthropic/mock only, digest bound, input cap), the
parse-plus-one-repair discipline, plan rules beyond the contract, provider
error propagation vs. repairable bad output, the pool transport, and token
accounting the orchestrator bills to the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.plan import Plan
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.errors import BudgetExceeded
from app.core.policies import Policies
from app.core.rubric import Rubric, resolve_sections
from app.planner import (
    LLMPlanner,
    PlannerError,
    PlannerOutputError,
    PlannerProviderNotAllowed,
    build_plan_request,
    load_planner_rules,
)
from app.providers.base import estimate_tokens
from app.providers.errors import ProviderTimeout
from app.providers.mock import FailureMode, MockProvider, Script
from tests.factories import SAMPLE_BRIEF, sample_ulid

CHAIN = (
    AgentRole.ARCHITECT,
    AgentRole.RESEARCHER,
    AgentRole.WRITER,
    AgentRole.REVIEWER,
    AgentRole.VALIDATOR,
)


def brief(**overrides: Any) -> Brief:
    payload = dict(SAMPLE_BRIEF)
    payload.update(overrides)
    return Brief.model_validate(payload)


def make_task(
    seed: int,
    role: AgentRole,
    *,
    depends_on: tuple[str, ...] = (),
    section_ids: tuple[str, ...] = ("introduction",),
    token_budget: int = 5_000,
) -> Task:
    return Task(
        id=sample_ulid(seed),
        title=f"{role.value} step",
        role=role,
        section_ids=list(section_ids),
        instructions=f"Do the {role.value} work for the introduction.",
        acceptance=["The acceptance list is satisfied"],
        token_budget=token_budget,
        depends_on=list(depends_on),
    )


def make_plan(
    *,
    version: int = 1,
    seed_base: int = 100,
    roles: tuple[AgentRole, ...] = CHAIN,
    token_budget: int = 5_000,
    section_ids: tuple[str, ...] = ("introduction",),
) -> Plan:
    tasks: list[Task] = []
    previous: str | None = None
    for index, role in enumerate(roles):
        task = make_task(
            seed_base + index,
            role,
            depends_on=(previous,) if previous else (),
            section_ids=section_ids,
            token_budget=token_budget,
        )
        tasks.append(task)
        previous = task.id
    return Plan(version=version, goal="A finished, verified article.", tasks=tasks)


def sections_for(rubric: Rubric):  # type: ignore[no-untyped-def]
    return resolve_sections(rubric, brief().article_type)


def report() -> QualityReport:
    return QualityReport(
        iter=1,
        gates=dict.fromkeys(GATE_NAMES, True),
        Q=85,
        verified_citation_ratio=1.0,
    )


class NamedProvider:
    """A provider with a forbidden name; the planner must refuse it unseen."""

    name: ClassVar[str] = "openai_compatible"

    async def generate(self, request: LLMRequest, **_: Any) -> LLMResponse:
        raise AssertionError("a refused provider must never be called")


class FakePool:
    """The pool transport shape: role + user_id, resolves accounts itself."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def generate(
        self, request: LLMRequest, *, role: str, user_id: str, model: str | None = None
    ) -> LLMResponse:
        _ = (request, model)
        self.calls.append((role, user_id))
        return LLMResponse(
            text=self._text,
            usage=TokenUsage.model_validate({"in": 40, "out": 10, "cached": 5}),
            model="claude-test",
            latency_ms=1,
        )


# --------------------------------------------------------------------------- guardrails
def test_planner_refuses_a_non_anthropic_provider() -> None:
    with pytest.raises(PlannerProviderNotAllowed):
        LLMPlanner(NamedProvider())  # type: ignore[arg-type]


def test_planner_needs_exactly_one_transport() -> None:
    provider = MockProvider()
    with pytest.raises(PlannerError):
        LLMPlanner()
    with pytest.raises(PlannerError):
        LLMPlanner(provider, pool=FakePool("{}"), user_id="user-1")
    with pytest.raises(PlannerError):
        LLMPlanner(pool=FakePool("{}"))  # a pool without a user resolves nobody


async def test_digest_over_the_bound_is_refused_before_any_call() -> None:
    provider = MockProvider()
    planner = LLMPlanner(provider)
    oversized = "word " * 10_000  # ~2500 tokens, over the 2000 bound
    with pytest.raises(PlannerError):
        await planner.plan(brief(), digest_text=oversized)
    assert provider.call_count == 0


async def test_prompt_over_the_input_cap_is_refused_not_truncated() -> None:
    policies = Policies(
        run_token_cap=100_000, iteration_token_cap=5_000, call_input_cap=700, digest_max_tokens=200
    )
    provider = MockProvider()
    planner = LLMPlanner(provider, policies=policies)
    long_brief = brief(provided_data=["evidence " * 2_000])
    with pytest.raises(BudgetExceeded):
        await planner.plan(long_brief)
    assert provider.call_count == 0


def test_request_carries_the_plan_schema_and_references_only(rubric: Rubric) -> None:
    request = build_plan_request(
        brief(), sections=sections_for(rubric), version=3, policies=Policies()
    )
    assert request.json_schema is not None
    text = request.messages[0].text
    assert "version 3" in text
    assert "introduction" in text and "related-work" in text
    # The planner prompt must state its hard rules, not assume them.
    system = request.system_blocks[0].text.lower()
    assert "never write article text" in system
    assert "needs_user_input" in system


# --------------------------------------------------------------------------- planning
async def test_plan_success_and_token_accounting(rubric: Rubric) -> None:
    plan = make_plan()
    provider = MockProvider(Script().reply("planner", 0, plan.model_dump_json()))
    planner = LLMPlanner(provider)

    produced = await planner.plan(brief(), sections=sections_for(rubric), version=1)

    assert produced == plan
    assert provider.call_count == 1
    assert provider.calls[0].role == "planner"
    assert planner.tokens_spent == provider.total_tokens().total > 0


async def test_plan_accepts_a_fenced_json_reply(rubric: Rubric) -> None:
    plan = make_plan()
    fenced = "```json\n" + plan.model_dump_json() + "\n```"
    provider = MockProvider(Script().reply("planner", 0, fenced))
    produced = await LLMPlanner(provider).plan(brief(), sections=sections_for(rubric), version=1)
    assert produced == plan


async def test_bad_reply_is_repaired_exactly_once(rubric: Rubric) -> None:
    plan = make_plan()
    script = (
        Script().reply("planner", 0, "not json at all").reply("planner", 1, plan.model_dump_json())
    )
    provider = MockProvider(script)
    planner = LLMPlanner(provider)

    produced = await planner.plan(brief(), sections=sections_for(rubric), version=1)

    assert produced == plan
    assert provider.call_count == 2
    # The failed attempt was still billed.
    assert planner.tokens_spent == provider.total_tokens().total


async def test_two_bad_replies_raise_output_error() -> None:
    script = Script().reply("planner", 0, "no").reply("planner", 1, "still no")
    provider = MockProvider(script)
    with pytest.raises(PlannerOutputError):
        await LLMPlanner(provider).plan(brief(), version=1)
    assert provider.call_count == 2  # one call + one repair, never a loop


async def test_malformed_json_provider_failure_is_repairable(rubric: Rubric) -> None:
    plan = make_plan()
    script = (
        Script()
        .fail("planner", 0, FailureMode.MALFORMED_JSON)
        .reply("planner", 1, plan.model_dump_json())
    )
    provider = MockProvider(script)
    produced = await LLMPlanner(provider).plan(brief(), sections=sections_for(rubric), version=1)
    assert produced == plan
    assert provider.call_count == 2


async def test_provider_timeout_is_not_repaired_away() -> None:
    provider = MockProvider(Script().fail("planner", 0, FailureMode.TIMEOUT))
    planner = LLMPlanner(provider)
    with pytest.raises(ProviderTimeout):
        await planner.plan(brief(), version=1)
    assert provider.call_count == 1
    assert planner.tokens_spent == 0


async def test_plan_with_missing_required_role_is_repaired(rubric: Rubric) -> None:
    thin = make_plan(seed_base=300, roles=(AgentRole.WRITER, AgentRole.VALIDATOR))
    full = make_plan(seed_base=400)
    script = (
        Script()
        .reply("planner", 0, thin.model_dump_json())
        .reply("planner", 1, full.model_dump_json())
    )
    provider = MockProvider(script)
    produced = await LLMPlanner(provider).plan(brief(), sections=sections_for(rubric), version=1)
    assert produced == full
    assert provider.call_count == 2


async def test_plan_with_wrong_version_is_rejected(rubric: Rubric) -> None:
    wrong = make_plan(version=7)
    right = make_plan(version=2, seed_base=500)
    script = (
        Script()
        .reply("planner", 0, wrong.model_dump_json())
        .reply("planner", 1, right.model_dump_json())
    )
    provider = MockProvider(script)
    produced = await LLMPlanner(provider).replan(
        brief(), digest_text="# digest", sections=sections_for(rubric), version=2
    )
    assert produced.version == 2


async def test_plan_inventing_a_section_is_rejected(rubric: Rubric) -> None:
    invented = make_plan(seed_base=600, section_ids=("conclusions-and-vibes",))
    honest = make_plan(seed_base=700)
    script = (
        Script()
        .reply("planner", 0, invented.model_dump_json())
        .reply("planner", 1, honest.model_dump_json())
    )
    provider = MockProvider(script)
    produced = await LLMPlanner(provider).plan(brief(), sections=sections_for(rubric), version=1)
    assert produced == honest


async def test_plan_budgets_over_the_iteration_cap_are_rejected(rubric: Rubric) -> None:
    greedy = make_plan(seed_base=800, token_budget=40_000)  # 5 * 40k > 120k cap
    script = Script().reply("planner", 0, greedy.model_dump_json()).reply("planner", 1, "nope")
    provider = MockProvider(script)
    with pytest.raises(PlannerOutputError):
        await LLMPlanner(provider).plan(brief(), sections=sections_for(rubric), version=1)


async def test_pool_transport_resolves_through_the_planner_role(rubric: Rubric) -> None:
    plan = make_plan()
    pool = FakePool(plan.model_dump_json())
    planner = LLMPlanner(pool=pool, user_id="user-42")

    produced = await planner.plan(brief(), sections=sections_for(rubric), version=1)

    assert produced == plan
    assert pool.calls == [("planner", "user-42")]
    assert planner.tokens_spent == 55  # 40 in + 5 cached + 10 out


# --------------------------------------------------------------------------- evaluating
async def test_evaluate_returns_a_verdict() -> None:
    verdict = Verdict(meets_minimum=True, recommendation="finish")
    provider = MockProvider(Script().reply("planner", 0, verdict.model_dump_json()))
    produced = await LLMPlanner(provider).evaluate(brief(), report())
    assert produced == verdict


async def test_evaluate_rejects_a_contradictory_verdict() -> None:
    # finish + meets_minimum=false violates the Verdict contract itself.
    bad = '{"meets_minimum": false, "recommendation": "finish"}'
    good_verdict = Verdict(
        meets_minimum=False, missing=["Verify the references"], recommendation="continue"
    )
    script = Script().reply("planner", 0, bad).reply("planner", 1, good_verdict.model_dump_json())
    provider = MockProvider(script)
    produced = await LLMPlanner(provider).evaluate(brief(), report())
    assert produced == good_verdict
    assert provider.call_count == 2


async def test_unscripted_mock_stub_is_not_mistaken_for_a_plan() -> None:
    provider = MockProvider()  # no script: replies are stubs, not plans
    with pytest.raises(PlannerOutputError):
        await LLMPlanner(provider).plan(brief(), version=1)


def test_planner_rules_asset_exists_and_fits_the_budget(repo_root: Path) -> None:
    rules = load_planner_rules(repo_root / "rules")
    assert rules is not None
    assert "never write" in rules.lower()
    planner_rules = (repo_root / "rules" / "planner.rules.md").read_text(encoding="utf-8")
    assert estimate_tokens(planner_rules) <= 400
    assert load_planner_rules(repo_root / "no-such-dir") is None
