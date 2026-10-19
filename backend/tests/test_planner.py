"""Tests for the Planner pipeline (:class:`app.planner.Planner`).

MockProvider only (HARD RULE 5): scripted replies keyed by
``(BindingRole.PLANNER, call_index)``, failure injection for timeout /
malformed JSON, and call-log assertions. Covers the bounded repair loop,
refusing over-budget contexts before any spend, verdict guardrails applied to
model output, and the ProviderPool adapter seam.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, BindingRole, Recommendation
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.quality import QualityReport
from app.contracts.source import SourceCard
from app.core.errors import BudgetExceeded
from app.core.ids import UlidFactory, is_ulid
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.memory.digest import DigestSnapshot
from app.planner import (
    Planner,
    PlannerConfig,
    PlannerContext,
    PlannerContextTooLarge,
    PlannerError,
    PlannerOutputError,
    PoolBackedProvider,
    build_fallback_plan,
)
from app.providers.base import prompt_tokens
from app.providers.errors import ProviderTimeout
from app.providers.mock import FailureMode, MockProvider, Script
from tests.factories import FIXED_TS_MS, SAMPLE_BRIEF, sample_payloads
from tests.test_planner_validation import SOURCE_ID, full_plan_payload, task


def seeded_factory() -> UlidFactory:
    """Deterministic ids for every test."""
    return UlidFactory(timestamp_ms=FIXED_TS_MS, entropy=b"\x02" * 10)


def make_context(rubric: Rubric, **overrides: Any) -> PlannerContext:
    """Planner context for the sample brief, with the fixture source card present.

    ``full_plan_payload`` references ``SOURCE_ID``; a context without that card
    would (correctly) trigger the invented-source semantic check.
    """
    card_payload = dict(sample_payloads()["SourceCard"])
    card_payload["id"] = SOURCE_ID
    fields: dict[str, Any] = {
        "brief": Brief.model_validate(SAMPLE_BRIEF),
        "rubric": rubric,
        "sources": (SourceCard.model_validate(card_payload),),
    }
    fields.update(overrides)
    return PlannerContext(**fields)


def verdict_payload(**overrides: Any) -> dict[str, Any]:
    """A valid 'keep going' verdict as a model would emit it."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "meets_minimum": False,
        "missing": ["A reference entry for (Petrova et al., 2023)."],
        "risks": ["Literature coverage is thin."],
        "recommendation": "continue",
    }
    payload.update(overrides)
    return payload


# -- planning -------------------------------------------------------------------


async def test_plan_initial_happy_path(rubric: Rubric) -> None:
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, full_plan_payload()))
    planner = Planner(provider, id_factory=seeded_factory())

    outcome = await planner.plan_initial(make_context(rubric))

    assert outcome.plan.version == 1  # model said 7; the server stamps versions
    assert outcome.attempts == 1 and outcome.repairs_used == 0
    assert outcome.model == "mock-standard-1"
    assert all(is_ulid(item.id) for item in outcome.plan.tasks)
    writer = outcome.plan.of_role(AgentRole.WRITER)[0]
    researcher = outcome.plan.of_role(AgentRole.RESEARCHER)[0]
    assert writer.depends_on == [researcher.id]  # T-labels remapped, not lost
    calls = provider.calls_for(BindingRole.PLANNER)
    assert len(calls) == 1 and calls[0].served_scripted
    assert outcome.usage.total == provider.total_tokens().total


async def test_replan_bumps_version_and_requires_only_validator(rubric: Rubric) -> None:
    previous = build_fallback_plan(
        Brief.model_validate(SAMPLE_BRIEF), rubric, id_factory=seeded_factory(), budget_cap=60_000
    )
    payload = {
        "schema_version": 1,
        "version": 1,
        "goal": "Fix the introduction and re-score.",
        "tasks": [
            task("T1", "Rewrite intro opening", "writer", [], section_ids=["introduction"]),
            task("T2", "Re-validate", "validator", ["T1"]),
        ],
    }
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, payload))
    planner = Planner(provider, id_factory=seeded_factory())

    outcome = await planner.replan(make_context(rubric, previous_plan=previous))

    assert outcome.plan.version == previous.version + 1


async def test_replan_without_previous_plan_fails_before_spending(rubric: Rubric) -> None:
    provider = MockProvider()
    with pytest.raises(PlannerError):
        await Planner(provider).replan(make_context(rubric))
    assert provider.call_count == 0


async def test_repair_fixes_missing_role_and_bills_both_calls(rubric: Rubric) -> None:
    incomplete = full_plan_payload()
    incomplete["tasks"] = [item for item in incomplete["tasks"] if item["role"] != "validator"]
    incomplete["tasks"][2]["depends_on"] = ["T2"]  # writer still chained to researcher
    script = (
        Script()
        .json_reply(BindingRole.PLANNER, 0, incomplete)
        .json_reply(BindingRole.PLANNER, 1, full_plan_payload())
    )
    provider = MockProvider(script)
    planner = Planner(provider, id_factory=seeded_factory())

    outcome = await planner.plan_initial(make_context(rubric))

    assert outcome.attempts == 2 and outcome.repairs_used == 1
    assert any("validator" in issue for issue in outcome.repaired_issues)
    assert outcome.usage.total == provider.total_tokens().total


async def test_repair_exhausted_raises_with_issues_and_attempts(rubric: Rubric) -> None:
    bad = full_plan_payload()
    bad["tasks"] = [item for item in bad["tasks"] if item["role"] != "validator"]
    script = (
        Script().json_reply(BindingRole.PLANNER, 0, bad).json_reply(BindingRole.PLANNER, 1, bad)
    )
    provider = MockProvider(script)
    planner = Planner(provider, id_factory=seeded_factory())

    with pytest.raises(PlannerOutputError) as excinfo:
        await planner.plan_initial(make_context(rubric))

    assert excinfo.value.attempts == 2
    assert any("validator" in issue for issue in excinfo.value.issues)
    assert excinfo.value.body_preview is not None
    assert provider.call_count == 2


async def test_no_repairs_means_one_attempt(rubric: Rubric) -> None:
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, {"goal": "x"}))
    planner = Planner(provider, id_factory=seeded_factory(), config=PlannerConfig(max_repairs=0))
    with pytest.raises(PlannerOutputError) as excinfo:
        await planner.plan_initial(make_context(rubric))
    assert excinfo.value.attempts == 1


async def test_malformed_text_then_valid_json_repairs(rubric: Rubric) -> None:
    script = (
        Script()
        .reply(BindingRole.PLANNER, 0, "{this is not json")
        .json_reply(BindingRole.PLANNER, 1, full_plan_payload())
    )
    outcome = await Planner(MockProvider(script), id_factory=seeded_factory()).plan_initial(
        make_context(rubric)
    )
    assert outcome.repairs_used == 1


async def test_malformed_json_provider_failure_is_repairable(rubric: Rubric) -> None:
    """Mock failure injection raises ProviderBadResponse; the Planner repairs it."""
    script = (
        Script()
        .fail(BindingRole.PLANNER, 0, FailureMode.MALFORMED_JSON)
        .json_reply(BindingRole.PLANNER, 1, full_plan_payload())
    )
    provider = MockProvider(script)
    outcome = await Planner(provider, id_factory=seeded_factory()).plan_initial(
        make_context(rubric)
    )
    assert outcome.repairs_used == 1
    assert len(provider.failed_calls) == 1


async def test_provider_timeout_propagates_without_repair(rubric: Rubric) -> None:
    provider = MockProvider(Script().fail(BindingRole.PLANNER, 0, FailureMode.TIMEOUT))
    with pytest.raises(ProviderTimeout):
        await Planner(provider, id_factory=seeded_factory()).plan_initial(make_context(rubric))
    assert provider.call_count == 1  # retries are the pool/orchestrator's job


async def test_unscripted_mock_reply_fails_loudly(rubric: Rubric) -> None:
    provider = MockProvider()
    with pytest.raises(PlannerOutputError):
        await Planner(provider, id_factory=seeded_factory()).plan_initial(make_context(rubric))
    assert provider.call_count == 2  # one attempt + one bounded repair, never a stub plan


# -- budget refusal ---------------------------------------------------------------


async def test_oversized_digest_is_refused_before_any_call(rubric: Rubric) -> None:
    provider = MockProvider()
    context = make_context(rubric, digest="history " * 5_000)
    with pytest.raises(PlannerContextTooLarge) as excinfo:
        await Planner(provider).plan_initial(context)
    assert excinfo.value.parts["digest"] > excinfo.value.cap
    assert provider.call_count == 0


async def test_digest_snapshot_is_accepted_as_history(rubric: Rubric) -> None:
    snapshot = DigestSnapshot(
        markdown="# run digest\n- drafted introduction",
        tokens=12,
        max_tokens=2_000,
        compact_at_tokens=1_600,
    )
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, full_plan_payload()))
    outcome = await Planner(provider, id_factory=seeded_factory()).plan_initial(
        make_context(rubric, digest=snapshot)
    )
    assert outcome.plan.version == 1


async def test_input_cap_is_refused_with_part_breakdown(rubric: Rubric) -> None:
    small = Policies(
        run_token_cap=10_000,
        iteration_token_cap=1_000,
        call_input_cap=500,
        digest_max_tokens=200,
    )
    provider = MockProvider()
    with pytest.raises(PlannerContextTooLarge) as excinfo:
        await Planner(provider, policies=small).plan_initial(make_context(rubric))
    assert excinfo.value.cap == 500
    assert excinfo.value.parts["system"] > 0 and excinfo.value.parts["messages"] > 0
    assert provider.call_count == 0


class RecordingGuard:
    """A BudgetGuard double: records prechecks, optionally refusing."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.roles: list[str] = []

    def precheck(self, request: LLMRequest, *, role: str) -> None:
        self.roles.append(role)
        _ = prompt_tokens(request)
        if self.refuse:
            raise BudgetExceeded("planner precheck refused by test guard")


async def test_budget_guard_is_consulted_and_can_refuse(rubric: Rubric) -> None:
    guard = RecordingGuard(refuse=True)
    provider = MockProvider()
    with pytest.raises(BudgetExceeded):
        await Planner(provider, budget_guard=guard).plan_initial(make_context(rubric))
    assert guard.roles == [BindingRole.PLANNER.value]
    assert provider.call_count == 0

    allowing = RecordingGuard()
    ok_provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, full_plan_payload()))
    outcome = await Planner(
        ok_provider, id_factory=seeded_factory(), budget_guard=allowing
    ).plan_initial(make_context(rubric))
    assert outcome.attempts == 1 and allowing.roles == [BindingRole.PLANNER.value]


# -- evaluation -------------------------------------------------------------------


async def test_evaluate_happy_path(rubric: Rubric) -> None:
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, verdict_payload()))
    outcome = await Planner(provider, id_factory=seeded_factory()).evaluate(make_context(rubric))
    assert outcome.verdict.recommendation is Recommendation.CONTINUE
    assert outcome.guardrail_adjustments == ()
    assert outcome.usage.total == provider.total_tokens().total


async def test_evaluate_guardrails_override_optimistic_model(rubric: Rubric) -> None:
    report = QualityReport.model_validate(sample_payloads()["QualityReport"])  # Q=67, high issue
    optimistic = verdict_payload(meets_minimum=True, missing=[], recommendation="finish")
    provider = MockProvider(Script().json_reply(BindingRole.PLANNER, 0, optimistic))

    outcome = await Planner(provider, id_factory=seeded_factory()).evaluate(
        make_context(rubric, report=report)
    )

    assert outcome.guardrail_adjustments  # at least q-below-minimum / high-issue override
    assert outcome.verdict.meets_minimum is False
    assert outcome.verdict.recommendation is Recommendation.CONTINUE
    assert outcome.verdict.missing


async def test_evaluate_repairs_contradictory_verdict(rubric: Rubric) -> None:
    contradictory = verdict_payload(recommendation="finish")  # meets_minimum=False
    script = (
        Script()
        .json_reply(BindingRole.PLANNER, 0, contradictory)
        .json_reply(BindingRole.PLANNER, 1, verdict_payload())
    )
    outcome = await Planner(MockProvider(script), id_factory=seeded_factory()).evaluate(
        make_context(rubric)
    )
    assert outcome.repairs_used == 1
    assert any("contradicts" in issue for issue in outcome.repaired_issues)


def test_planner_config_bounds_are_enforced() -> None:
    with pytest.raises(PlannerError):
        PlannerConfig(max_repairs=9)
    with pytest.raises(PlannerError):
        PlannerConfig(plan_max_output_tokens=10)
    with pytest.raises(PlannerError):
        PlannerConfig(initial_required_roles=frozenset())


# -- pool adapter -------------------------------------------------------------------


class FakePool:
    """ProviderPool double recording the call-time resolution arguments."""

    def __init__(self, scripted_text: str) -> None:
        self.scripted_text = scripted_text
        self.seen: list[tuple[str, str, str | None]] = []
        self.closed = False
        self._inner = MockProvider()

    async def generate(
        self,
        request: LLMRequest,
        *,
        role: str,
        user_id: str,
        model: str | None = None,
    ) -> LLMResponse:
        self.seen.append((role, user_id, model))
        self._inner.script_reply(role, len(self.seen) - 1, self.scripted_text)
        return await self._inner.generate(request, model=model, role=role)

    async def aclose(self) -> None:
        self.closed = True


async def test_pool_backed_provider_resolves_per_call(rubric: Rubric) -> None:
    pool = FakePool(json.dumps(full_plan_payload()))
    provider = PoolBackedProvider(pool, user_id="user-001")
    planner = Planner(provider, id_factory=seeded_factory(), model="claude-tier-strong")

    outcome = await planner.plan_initial(make_context(rubric))

    assert outcome.plan.version == 1
    assert pool.seen == [(BindingRole.PLANNER.value, "user-001", "claude-tier-strong")]
    request = LLMRequest(
        system_blocks=[{"text": "system", "cache": True}],
        messages=[{"role": "user", "text": "hello planner"}],
        max_output_tokens=64,
    )
    assert await provider.count_tokens(request) == prompt_tokens(request)
    await provider.aclose()
    assert pool.closed is True
