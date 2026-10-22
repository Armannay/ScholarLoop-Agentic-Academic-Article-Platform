"""Nested budgets: hierarchy, atomicity, soft warnings, hard stops, persistence.

The concurrency test is the load-bearing one: twenty threads race for ten slots and the
committed total must land exactly on the cap, never above it. The store's lock covers the
whole read-modify-write, so the result is not best-effort - it is arithmetic.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.contracts.account import MonthlyBudget
from app.contracts.enums import AgentRole
from app.contracts.ledger import TokenUsage
from app.contracts.task import Task
from app.core.errors import BudgetExceeded
from app.governor import (
    BudgetEstimate,
    BudgetGovernor,
    BudgetLevel,
    BudgetLimits,
    BudgetScope,
    BudgetStoreError,
    FileBudgetStore,
    InMemoryBudgetStore,
    LevelUsage,
    Reservation,
)
from tests.factories import make_request, sample_ulid

pytestmark = pytest.mark.governor

#: run 1000 / iteration 400 / call 100 - small enough to reason about by hand.
LIMITS = BudgetLimits(run_tokens=1_000, iteration_tokens=400, call_input_tokens=100)


def make_governor(
    *,
    limits: BudgetLimits = LIMITS,
    run_id: str = "run-1",
    iteration: int = 0,
    account_id: str | None = None,
    period: str = "",
    store: object | None = None,
    on_soft_warning: object | None = None,
) -> BudgetGovernor:
    """A governor whose scope is explicit, so no test depends on the wall clock."""
    return BudgetGovernor(
        limits,
        scope=BudgetScope(run_id=run_id, iteration=iteration, account_id=account_id, period=period),
        store=store,  # type: ignore[arg-type]
        on_soft_warning=on_soft_warning,  # type: ignore[arg-type]
    )


def usage(in_: int, out: int = 0, cached: int = 0) -> TokenUsage:
    """TokenUsage with the wire aliases the contract uses."""
    return TokenUsage(**{"in": in_, "out": out, "cached": cached})


def make_task(*, token_budget: int = 2_000) -> Task:
    """A minimal valid Task for the budget guards."""
    return Task(
        id=sample_ulid(1),
        title="Draft the introduction",
        role=AgentRole.WRITER,
        section_ids=["introduction"],
        instructions="Draft 300 words from the provided source cards.",
        acceptance=["The section cites SourceCard ids only."],
        token_budget=token_budget,
    )


# --------------------------------------------------------------------------- hierarchy
def test_precheck_reserves_and_record_settles():
    governor = make_governor()
    reservation = governor.precheck(BudgetEstimate(input_tokens=60, output_tokens=20))

    assert isinstance(reservation, Reservation)
    assert reservation.reserved_tokens == 80
    assert reservation.is_open
    # The reservation is already committed: a concurrent call cannot spend it twice.
    assert governor.status().run.used == 80

    status = governor.record(usage(50, 10), reservation=reservation)

    assert reservation.settled
    assert status.run.used == 60
    assert status.iteration.used == 60


def test_hard_stop_at_100_percent():
    limits = BudgetLimits(run_tokens=100, iteration_tokens=100, call_input_tokens=50)
    governor = make_governor(limits=limits)

    governor.precheck(BudgetEstimate(input_tokens=40, output_tokens=60))

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(BudgetEstimate(input_tokens=1))

    assert caught.value.context["level"] == BudgetLevel.RUN.value
    assert caught.value.context["remaining"] == 0
    assert caught.value.context["limit"] == 100
    assert governor.status().stopped


def test_iteration_limit_is_reported_before_the_run_limit():
    governor = make_governor()
    governor.precheck(BudgetEstimate(input_tokens=100, output_tokens=150))

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(BudgetEstimate(input_tokens=100, output_tokens=150))

    context = caught.value.context
    assert context["level"] == BudgetLevel.ITERATION.value
    assert context["limit"] == 400
    assert context["remaining"] == 400 - 250


def test_call_input_cap_is_enforced_per_call():
    governor = make_governor()

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(BudgetEstimate(input_tokens=101))

    assert caught.value.context["level"] == BudgetLevel.CALL.value
    assert caught.value.context["limit"] == 100
    # Nothing was reserved by a refused call.
    assert governor.status().run.used == 0


def test_task_token_budget_binds_a_single_call():
    governor = make_governor()

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(
            BudgetEstimate(input_tokens=30, output_tokens=30), task=make_task(token_budget=50)
        )
    # 60 projected fits the call cap (100 input) but not the task's 50-token grant.
    assert caught.value.context["level"] == BudgetLevel.CALL.value
    assert caught.value.context["limit"] == 50


def test_task_budget_that_fits_is_admitted():
    governor = make_governor()
    reservation = governor.precheck(
        BudgetEstimate(input_tokens=20, output_tokens=20), task=make_task(token_budget=100)
    )
    assert reservation.reserved_tokens == 40


def test_account_month_budget_is_the_outer_scope():
    limits = BudgetLimits(
        run_tokens=1_000,
        iteration_tokens=400,
        call_input_tokens=100,
        account_month_tokens=100,
    )
    governor = make_governor(limits=limits, account_id="01ACCOUNT", period="2026-10")
    governor.precheck(BudgetEstimate(input_tokens=60))

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(BudgetEstimate(input_tokens=50))

    assert caught.value.context["level"] == BudgetLevel.ACCOUNT_MONTH.value
    assert caught.value.context["remaining"] == 40


def test_usd_budget_refusal_reports_usd_not_zero_tokens():
    limits = BudgetLimits(
        run_tokens=1_000,
        iteration_tokens=400,
        call_input_tokens=100,
        account_month_usd=1.0,
    )
    governor = make_governor(limits=limits, account_id="01ACCOUNT", period="2026-10")
    governor.precheck(BudgetEstimate(input_tokens=10, cost_usd=0.6))

    with pytest.raises(BudgetExceeded) as caught:
        governor.precheck(BudgetEstimate(input_tokens=10, cost_usd=0.5))

    context = caught.value.context
    assert context["level"] == BudgetLevel.ACCOUNT_MONTH.value
    # The ceiling that bound is USD: reporting limit=0 and used=<tokens> would be a lie.
    assert context["unit"] == "usd"
    assert context["limit"] == pytest.approx(1.0)
    assert context["used"] == pytest.approx(0.6)  # committed
    assert context["projected"] == pytest.approx(1.1)  # committed + this call
    assert context["remaining"] == pytest.approx(0.4)
    assert "$" in caught.value.message


def test_limits_from_policies_and_account_budget():
    limits = BudgetLimits.from_policies()
    assert limits.account_month_tokens is None
    assert limits.run_tokens > limits.iteration_tokens > limits.call_input_tokens

    capped = BudgetLimits.from_policies(monthly_budget=MonthlyBudget(tokens=5_000, usd=2.0))
    assert capped.account_month_tokens == 5_000
    assert capped.account_month_usd == 2.0


def test_limits_reject_inconsistent_nesting():
    with pytest.raises(ValueError, match="iteration_tokens"):
        BudgetLimits(run_tokens=100, iteration_tokens=200, call_input_tokens=50)
    with pytest.raises(ValueError, match="call_input_tokens"):
        BudgetLimits(run_tokens=100, iteration_tokens=100, call_input_tokens=100)
    with pytest.raises(ValueError, match="soft_warn_at"):
        BudgetLimits(run_tokens=100, iteration_tokens=90, call_input_tokens=50, soft_warn_at=1.5)


def test_an_account_budget_without_an_account_is_rejected():
    limits = BudgetLimits(
        run_tokens=1_000,
        iteration_tokens=400,
        call_input_tokens=100,
        account_month_tokens=5_000,
    )

    with pytest.raises(ValueError, match="account_id"):
        BudgetGovernor(limits, scope=BudgetScope(run_id="run-1"))


def test_status_reports_every_level_outer_scope_first():
    governor = make_governor(account_id="01ACCOUNT", period="2026-10")
    levels = [usage.level for usage in governor.status().levels]
    assert levels == list(BudgetLevel)


# --------------------------------------------------------------------------- warnings
def test_soft_warning_fires_once_at_eighty_percent():
    warnings: list[LevelUsage] = []
    limits = BudgetLimits(run_tokens=100, iteration_tokens=100, call_input_tokens=50)
    governor = make_governor(limits=limits, on_soft_warning=warnings.append)

    reservation = governor.precheck(BudgetEstimate(input_tokens=40, output_tokens=40))
    governor.record(usage(40, 40), reservation=reservation)
    governor.precheck(BudgetEstimate(input_tokens=10, output_tokens=5))

    # Run and iteration both sit at 80 % of an identical cap: each warns exactly once,
    # and the second call crossing neither threshold again does not repeat them.
    assert [warning.level for warning in warnings] == [BudgetLevel.RUN, BudgetLevel.ITERATION]
    assert all(warning.utilization >= 0.8 for warning in warnings)
    assert governor.status().warnings == (BudgetLevel.RUN, BudgetLevel.ITERATION)


# --------------------------------------------------------------------------- atomicity
def test_atomicity_under_concurrency():
    limits = BudgetLimits(run_tokens=1_000, iteration_tokens=1_000, call_input_tokens=500)
    governor = make_governor(limits=limits)
    admitted = 0
    refused = 0

    def attempt() -> None:
        nonlocal admitted, refused
        try:
            reservation = governor.precheck(BudgetEstimate(input_tokens=100))
        except BudgetExceeded:
            refused += 1
            return
        governor.record(usage(100), reservation=reservation)
        admitted += 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: attempt(), range(20)))

    assert admitted == 10
    assert refused == 10
    assert governor.status().run.used == 1_000


def test_release_refunds_an_unspent_reservation():
    governor = make_governor()
    reservation = governor.precheck(BudgetEstimate(input_tokens=50))
    assert governor.status().run.used == 50

    governor.release(reservation)
    assert reservation.settled
    assert governor.status().run.used == 0

    governor.release(reservation)  # settling twice must not go negative
    assert governor.status().run.used == 0


def test_recording_the_same_reservation_twice_does_not_inflate_the_budget():
    governor = make_governor()
    reservation = governor.precheck(BudgetEstimate(input_tokens=50))

    governor.record(usage(50), reservation=reservation)
    status = governor.record(usage(50), reservation=reservation)

    assert status.run.used == 50


def test_record_without_a_reservation_still_counts():
    governor = make_governor()
    status = governor.record(usage(25, 5, cached=10))
    assert status.run.used == 40


def test_advance_iteration_starts_a_fresh_iteration_counter():
    governor = make_governor()
    reservation = governor.precheck(BudgetEstimate(input_tokens=50))
    governor.record(usage(50), reservation=reservation)

    governor.advance_iteration(1)

    status = governor.status()
    assert status.run.used == 50
    assert status.iteration.used == 0
    with pytest.raises(ValueError, match="forward only"):
        governor.advance_iteration(0)


def test_usage_settles_to_the_iteration_that_made_the_call():
    governor = make_governor()
    reservation = governor.precheck(BudgetEstimate(input_tokens=40))
    governor.advance_iteration(1)

    status = governor.record(usage(40), reservation=reservation)

    # The returned snapshot describes the call's own scope, not the advanced one.
    assert status.scope.iteration == 0
    assert status.iteration.used == 40
    assert governor.status().iteration.used == 0  # iteration 1 made no call of its own


def test_precheck_accepts_an_llm_request_for_the_agent_runtime_seam():
    governor = make_governor()
    request = make_request(schema=True).model_copy(update={"max_output_tokens": 50})

    reservation = governor.precheck(request, role="writer", task=make_task())

    assert reservation.estimate.source == "heuristic"
    assert reservation.estimate.input_tokens > 0
    assert reservation.estimate.output_tokens == request.max_output_tokens


# --------------------------------------------------------------------------- persistence
def test_file_store_survives_a_new_governor(tmp_path: Path):
    path = tmp_path / "budget" / "counters.json"
    store = FileBudgetStore(path)
    first = make_governor(store=store, account_id="01ACCOUNT", period="2026-10")
    reservation = first.precheck(BudgetEstimate(input_tokens=50))
    first.record(usage(50), reservation=reservation)

    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))

    second = make_governor(store=FileBudgetStore(path), account_id="01ACCOUNT", period="2026-10")
    assert second.status().run.used == 50
    assert second.status().account_month.used == 50


def test_file_store_wraps_a_write_failure(tmp_path: Path):
    path = tmp_path / "counters.json"
    path.mkdir()  # the target is a directory: the atomic replace cannot succeed
    store = FileBudgetStore(path)

    with pytest.raises(BudgetStoreError):
        store.commit({"run:1": 1.0})


def test_file_store_rejects_a_corrupt_document(tmp_path: Path):
    path = tmp_path / "counters.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(BudgetStoreError):
        FileBudgetStore(path).snapshot()


def test_in_memory_store_can_be_seeded():
    store = InMemoryBudgetStore({"run:seeded": 42})
    governor = make_governor(store=store, run_id="seeded")
    assert governor.status().run.used == 42
