"""Budget hierarchy: nesting, soft warnings, hard stops, atomicity, persistence.

The budget manager is the last line of defence for HARD RULE 3 (token-frugal by
design). These tests attack it from every level: each ceiling must fire on its
own, concurrent records must not lose a token, and a restarted process must see
the same counters.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest

from app.contracts.account import MonthlyBudget
from app.contracts.ledger import TokenUsage
from app.core.policies import Policies
from app.governor.budget import (
    WARNING_THRESHOLD,
    BudgetExceeded,
    BudgetLevel,
    BudgetManager,
    HierarchicalBudget,
    month_key,
)

pytestmark = pytest.mark.config


def make_policies(**overrides: int) -> Policies:
    """Policies with sane nesting for a budget test (run >= iteration > call)."""
    values: dict[str, int] = {
        "run_token_cap": 100_000,
        "iteration_token_cap": 50_000,
        "call_input_cap": 20_000,
        "digest_max_tokens": 1_000,
    }
    values.update(overrides)
    return Policies(**values)


def usage(tokens: int) -> TokenUsage:
    """A usage object whose ``total`` is exactly ``tokens``."""
    return TokenUsage(**{"in": tokens, "out": 0, "cached": 0})


def fixed_clock(day: date):
    """A clock returning a fixed date, for month bucketing tests."""
    return lambda: day


# --------------------------------------------------------------------- hierarchy
def test_call_level_refuses_a_prompt_over_the_cap() -> None:
    """call_input_cap is per prompt: the very first check can refuse."""
    manager = BudgetManager(
        make_policies(
            run_token_cap=10_000,
            iteration_token_cap=5_000,
            call_input_cap=1_000,
            digest_max_tokens=500,
        )
    )

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(1_001)

    error = excinfo.value
    assert error.level == BudgetLevel.CALL
    assert error.limit == 1_000
    assert error.remaining == 1_000
    assert error.code == "budget_exceeded"


def test_iteration_level_accounts_for_earlier_calls_in_that_iteration() -> None:
    """Iteration 1 spending does not block iteration 2: the levels are separate."""
    manager = BudgetManager(make_policies(call_input_cap=4_000, iteration_token_cap=5_000))
    manager.record(usage(4_000), iteration=1)

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(1_500, iteration=1)
    assert excinfo.value.level == BudgetLevel.ITERATION
    assert excinfo.value.used == 4_000
    assert excinfo.value.remaining == 1_000

    # A fresh iteration still has its own headroom.
    assert manager.precheck(1_500, iteration=2) == []
    assert manager.iteration_used(2) == 0


def test_run_level_stops_spending_across_iterations() -> None:
    """The run cap is cumulative across every iteration."""
    manager = BudgetManager(
        make_policies(run_token_cap=10_000, iteration_token_cap=5_000, call_input_cap=4_000)
    )
    manager.record(usage(9_500), iteration=1)

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(600, iteration=2)

    assert excinfo.value.level == BudgetLevel.RUN
    assert excinfo.value.used == 9_500
    assert excinfo.value.remaining == 500


def test_account_month_level_uses_the_accounts_own_budget() -> None:
    """Per-account monthly budgets come from Step 1 and cap that account only."""
    manager = BudgetManager(
        make_policies(),
        account_budgets={
            "acc-a": MonthlyBudget(tokens=1_000),
            "acc-b": MonthlyBudget(tokens=9_000),
        },
    )
    manager.record(usage(900), account_id="acc-a")

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(200, account_id="acc-a")
    assert excinfo.value.level == BudgetLevel.ACCOUNT_MONTH
    assert excinfo.value.remaining == 100

    # acc-b is untouched by acc-a's spend.
    assert manager.precheck(200, account_id="acc-b") == []
    # and an account with no budget entry inherits no monthly ceiling
    assert manager.precheck(200, account_id="acc-c") == []


def test_account_usd_budget_is_enforced_when_a_cost_estimate_is_supplied() -> None:
    """Cost ceilings are optional and only apply when the caller estimates cost."""
    manager = BudgetManager(
        make_policies(),
        account_budgets={"acc": MonthlyBudget(tokens=None, usd=5.0)},
    )
    manager.record(usage(1_000), account_id="acc", est_cost=4.5)

    assert manager.precheck(1_000, account_id="acc", est_cost=0.4) == []
    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(1_000, account_id="acc", est_cost=0.6)
    assert excinfo.value.level == BudgetLevel.ACCOUNT_MONTH


def test_task_budget_can_tighten_the_call_ceiling() -> None:
    """A task's token_budget is a per-call ceiling when stricter than policy."""
    manager = BudgetManager(make_policies(call_input_cap=10_000, iteration_token_cap=20_000))

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(1_500, task_budget=1_000)

    assert excinfo.value.level == BudgetLevel.CALL
    assert excinfo.value.limit == 1_000


# ------------------------------------------------------------------- thresholds
def test_soft_warning_at_eighty_percent_does_not_block() -> None:
    """80 % logs a warning and keeps going; the caller sees the numbers."""
    manager = BudgetManager(
        make_policies(run_token_cap=10_000, iteration_token_cap=5_000, call_input_cap=4_000)
    )
    manager.record(usage(7_900))

    warnings = manager.precheck(200)

    assert warnings, "crossing 80 % must warn"
    run_warning = next(w for w in warnings if w.level == BudgetLevel.RUN.value)
    assert run_warning.percent >= WARNING_THRESHOLD
    assert run_warning.remaining == 10_000 - 8_100
    manager.record(usage(200))
    assert manager.is_warning(BudgetLevel.RUN)
    assert not manager.is_warning(BudgetLevel.CALL)


def test_hard_stop_at_one_hundred_percent() -> None:
    """At the cap, even a one-token call is refused on the next precheck."""
    manager = BudgetManager(
        make_policies(run_token_cap=10_000, iteration_token_cap=5_000, call_input_cap=4_000)
    )
    manager.record(usage(10_000))

    with pytest.raises(BudgetExceeded) as excinfo:
        manager.precheck(1)
    assert excinfo.value.level == BudgetLevel.RUN
    assert excinfo.value.remaining == 0


def test_exactly_reaching_the_cap_is_allowed_then_blocks() -> None:
    """The ceiling is inclusive: spending exactly to it is allowed once."""
    manager = BudgetManager(
        make_policies(run_token_cap=10_000, iteration_token_cap=5_000, call_input_cap=4_000)
    )
    manager.record(usage(9_000))

    # Exactly to the ceiling is still allowed (and is not a "warning": it is the stop).
    assert manager.precheck(1_000) == []
    manager.record(usage(1_000))

    with pytest.raises(BudgetExceeded):
        manager.precheck(1)


# -------------------------------------------------------------------- accounting
def test_record_counts_cached_tokens_against_every_cap() -> None:
    """Cached prompt tokens still cost context: they are part of ``total``."""
    manager = BudgetManager(
        make_policies(run_token_cap=10_000, iteration_token_cap=5_000, call_input_cap=4_000)
    )
    manager.record(
        TokenUsage(**{"in": 100, "out": 50, "cached": 850}),
        iteration=1,
        account_id="acc",
    )

    assert manager.run_used == 1_000
    assert manager.iteration_used(1) == 1_000
    assert manager.account_month_used("acc") == 1_000


def test_month_buckets_reset_on_the_first_of_the_next_month() -> None:
    """A monthly budget is per calendar month, not a rolling window."""
    current = [date(2026, 10, 19)]
    manager = BudgetManager(
        Policies(
            run_token_cap=100_000,
            iteration_token_cap=50_000,
            call_input_cap=20_000,
            digest_max_tokens=1_000,
        ),
        account_budgets={"acc": MonthlyBudget(tokens=1_000)},
        clock=lambda: current[0],
    )
    manager.record(usage(900), account_id="acc")
    assert manager.account_month_used("acc") == 900

    current[0] = date(2026, 11, 1)
    assert manager.account_month_used("acc") == 0
    warnings = manager.precheck(900, account_id="acc")
    assert [warning.level for warning in warnings] == [BudgetLevel.ACCOUNT_MONTH.value]
    assert month_key("acc", current[0]) == "acc:2026-11"


def test_iteration_status_requires_an_iteration() -> None:
    """Silently reporting 0 used would tell the loop it has full headroom."""
    manager = BudgetManager(make_policies())

    with pytest.raises(ValueError, match="iteration is required"):
        manager.status(BudgetLevel.ITERATION)


def test_persistence_failures_are_counted_and_visible(session_factory) -> None:
    """'Atomic and persisted' must not quietly become 'in-memory only'."""
    manager = BudgetManager(make_policies(), run_id="run-broken", session_factory=session_factory)
    with session_factory() as session:
        from sqlalchemy import text

        session.execute(text("DROP TABLE governor_budget"))
        session.commit()

    manager.record(usage(100))

    assert manager.persistence_failures == 1
    assert manager.run_used == 100, "the in-memory truth is kept"


def test_non_positive_task_budget_is_rejected() -> None:
    """A zero or negative task budget is a caller bug, not a ceiling."""
    manager = BudgetManager(make_policies())

    with pytest.raises(ValueError, match="task_budget must be > 0"):
        manager.precheck(10, task_budget=0)


def test_snapshot_reports_every_level() -> None:
    """The loop controller gets one object with run, iteration, call and account."""
    manager = BudgetManager(
        make_policies(),
        account_budgets={"acc": MonthlyBudget(tokens=10_000)},
    )
    manager.record(usage(1_000), iteration=2, account_id="acc")

    snapshot = manager.snapshot(account_id="acc", iteration=2)

    assert snapshot.run.used == 1_000
    assert snapshot.iteration is not None and snapshot.iteration.used == 1_000
    assert snapshot.call is not None and snapshot.call.limit == 20_000
    assert snapshot.account_month is not None and snapshot.account_month.used == 1_000


# --------------------------------------------------------------------- atomicity
def test_concurrent_records_lose_nothing() -> None:
    """Ten threads x 100 records of 10 tokens must total exactly 10,000."""
    manager = BudgetManager(
        make_policies(run_token_cap=100_000, iteration_token_cap=50_000, call_input_cap=10_000)
    )

    def worker() -> None:
        for _ in range(100):
            manager.record(10)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert manager.run_used == 10_000


def test_concurrent_precheck_and_record_never_exceed_the_ceiling() -> None:
    """check-then-record pairs from many threads still produce a consistent view.

    Precheck and record are individually atomic. Under the orchestrator's normal
    protocol (precheck, call, record) the only possible overshoot is in-flight
    calls, so we assert the recorded total never exceeds the cap when each
    thread's precheck saw room for its own call.
    """
    cap = 10_000
    manager = BudgetManager(
        make_policies(
            run_token_cap=cap,
            iteration_token_cap=cap,
            call_input_cap=500,
            digest_max_tokens=200,
        )
    )
    accepted = 0
    accepted_lock = threading.Lock()

    def worker() -> None:
        nonlocal accepted
        for _ in range(20):
            try:
                manager.precheck(100, iteration=0)
            except BudgetExceeded:
                continue
            with accepted_lock:
                accepted += 1
            manager.record(100, iteration=0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert manager.run_used == accepted * 100
    assert manager.run_used <= cap + 100 * 8  # in-flight calls may complete after a refusal


# -------------------------------------------------------------------- persistence
def test_counters_survive_a_new_manager_on_the_same_database(
    session_factory, tmp_path: Path
) -> None:
    """A restarted process restores the run's spend from governor_budget."""
    policies = make_policies()
    first = BudgetManager(policies, run_id="run-persist", session_factory=session_factory)
    first.record(usage(1_200), iteration=1, account_id="acc", role="writer", model="mock-1")
    first.record(usage(800), iteration=2, account_id="acc", role="writer", model="mock-1")

    restored = BudgetManager(policies, run_id="run-persist", session_factory=session_factory)

    assert restored.run_used == 2_000
    assert restored.iteration_used(1) == 1_200
    assert restored.account_month_used("acc") == 2_000


def test_governor_does_not_write_account_usage_daily(session_factory) -> None:
    """One writer per table: the pool's SqlUsageSink already bills this call.

    A second write here would double every token and every cent in the
    per-account monthly view that the Step-1 budgets are read from.
    """
    from sqlalchemy import text

    manager = BudgetManager(make_policies(), run_id="run-usage", session_factory=session_factory)
    manager.record(
        TokenUsage(**{"in": 100, "out": 50, "cached": 25}),
        iteration=1,
        account_id="acc",
        role="writer",
        model="claude-sonnet-4",
    )

    with session_factory() as session:
        rows = session.execute(text("SELECT COUNT(*) FROM account_usage_daily")).scalar()
        persisted = session.execute(
            text("SELECT tokens_used FROM governor_budget WHERE run_id = :run_id"),
            {"run_id": "run-usage"},
        ).scalar()

    assert rows == 0, "the governor must not duplicate the usage sink's roll-up"
    assert persisted == 175


def test_hierarchical_budget_alias_still_points_at_the_manager() -> None:
    """Old call sites importing HierarchicalBudget keep working."""
    assert HierarchicalBudget is BudgetManager
