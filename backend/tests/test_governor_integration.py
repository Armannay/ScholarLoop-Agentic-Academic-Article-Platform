"""The governor's public surface, composed end-to-end with MockProvider.

HARD RULE 5: no live provider. This test is the integration contract the
orchestrator will follow for every call:

    estimate -> precheck -> provider.generate -> record -> telemetry -> reconcile

It proves the pieces agree on the same token totals, which is the only way the
budget can be trusted.
"""

from __future__ import annotations

import pytest

from app.contracts.account import MonthlyBudget
from app.contracts.brief import Brief
from app.contracts.task import Task
from app.governor.budget import BudgetExceeded, BudgetManager
from app.governor.context_builder import RunState, build
from app.governor.estimate import TokenEstimator
from app.governor.telemetry import TelemetryStore
from app.providers.mock import MockProvider, Script
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.config

ROOT_RULES = "## Never\n1. Never fabricate sources.\n2. Never print an API key.\n"


def make_task() -> Task:
    """A single writer task over the introduction."""
    return Task(
        id=sample_ulid(0),
        title="Draft the introduction",
        role="writer",
        section_ids=["introduction"],
        source_ids=[],
        instructions="Open with the gap, then state the contribution.",
        acceptance=["No unsupported claims"],
        token_budget=6_000,
        depends_on=[],
    )


def make_state() -> RunState:
    """A run state that fits comfortably inside the shipped ceilings."""
    return RunState(
        run_id="run-integration",
        iteration=1,
        role="writer",
        brief=Brief(**SAMPLE_BRIEF),
        root_rules=ROOT_RULES,
        project_rules="## Project\n- Prefer primary sources.",
        role_rules="## Writer\n- Prefer small patches.",
        rubric_excerpt="Gates: no_placeholders, citations_resolve_both_ways",
        skills=[],
        selected_skills=[],
        source_cards=[],
        digest="Iter 0: plan accepted",
        section_text="THE DRAFT SO FAR.",
    )


async def test_one_governed_call_from_estimate_to_reconcile(policies) -> None:
    """The full loop: estimate, precheck, call, record, telemetry, drift."""
    provider = MockProvider(Script().reply("writer", 0, '{"text": "draft"}'))
    estimator = TokenEstimator(provider)
    budget = BudgetManager(
        policies,
        account_budgets={"acc-1": MonthlyBudget(tokens=100_000)},
        run_id="run-integration",
    )
    telemetry = TelemetryStore()

    task = make_task()
    request = build(task, make_state())
    estimate = await estimator.estimate(request)
    assert estimate > 0

    # 1. Check before spending: prompt plus the answer we are about to buy.
    budget.precheck(
        estimate + request.max_output_tokens,
        account_id="acc-1",
        iteration=1,
        task_budget=6_000,
    )

    # 2. Spend.
    response = await provider.generate(request, model="mock-standard-1", role="writer")
    assert response.text == '{"text": "draft"}'
    assert provider.calls and provider.calls[0].served_scripted

    # 3. Record exactly what the provider reported.
    budget.record(
        response.usage,
        account_id="acc-1",
        iteration=1,
        role="writer",
        model=response.model,
    )
    telemetry.record_call(
        run_id="run-integration",
        iter=1,
        task_id=task.id,
        role="writer",
        model=response.model,
        usage=response.usage,
        latency_ms=response.latency_ms,
    )
    drift = estimator.reconcile(estimate, response.usage)

    # 4. Every component agrees on the same total.
    assert budget.run_used == response.usage.total
    assert budget.iteration_used(1) == response.usage.total
    assert budget.account_month_used("acc-1") == response.usage.total
    assert telemetry.total_tokens("run-integration") == response.usage.total
    assert telemetry.tokens_by_role("run-integration") == {"writer": response.usage.total}
    assert telemetry.tokens_by_iteration("run-integration") == {1: response.usage.total}
    assert drift.actual == response.usage.total
    assert estimator.drift_stats.count == 1


async def test_refused_precheck_means_the_provider_is_never_called(policies) -> None:
    """A budget stop is a *stop*: no tokens are burned to discover it."""
    provider = MockProvider(Script().reply("writer", 0, '{"text": "draft"}'))
    estimator = TokenEstimator(provider)
    budget = BudgetManager(
        policies,
        account_budgets={"acc-1": MonthlyBudget(tokens=1_000)},
    )
    budget.record(990, account_id="acc-1")

    request = build(make_task(), make_state())
    estimate = await estimator.estimate(request)

    with pytest.raises(BudgetExceeded) as excinfo:
        budget.precheck(estimate, account_id="acc-1", iteration=1)

    assert excinfo.value.level == "account_month"
    assert provider.calls == [], "the refusal must happen before the network"
