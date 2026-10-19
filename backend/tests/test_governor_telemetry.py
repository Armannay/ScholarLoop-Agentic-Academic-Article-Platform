"""Telemetry: per-call records, the queries the loop needs, and token ROI.

ROI is the erosion detector's fuel gauge: "quality gained per 10k tokens". A
wrong denominator here silently changes loop behaviour, so the arithmetic is
pinned explicitly.
"""

from __future__ import annotations

import pytest

from app.contracts.ledger import TokenUsage
from app.contracts.quality import GATE_NAMES, QualityReport
from app.governor.telemetry import (
    CallRecord,
    TelemetryStore,
    cache_hit_ratio_for,
    token_roi,
    token_roi_from_reports,
)

pytestmark = pytest.mark.config


def report(iter_: int, q: int) -> QualityReport:
    """A minimal valid QualityReport for ROI windows."""
    return QualityReport(
        iter=iter_,
        gates=dict.fromkeys(GATE_NAMES, True),
        scores={"coherence": q},
        Q=q,
        verified_citation_ratio=0.5,
        issues=[],
    )


def store_with_calls() -> TelemetryStore:
    """Three calls across two roles and two iterations."""
    store = TelemetryStore()
    store.record_call(
        run_id="run-1",
        iter=1,
        task_id="task-a",
        role="writer",
        model="claude-sonnet-4",
        usage=TokenUsage(**{"in": 1_000, "out": 500, "cached": 200}),
        latency_ms=100,
    )
    store.record_call(
        run_id="run-1",
        iter=1,
        task_id="task-a",
        role="researcher",
        model="claude-haiku-4-5",
        usage=TokenUsage(**{"in": 800, "out": 200, "cached": 0}),
        latency_ms=300,
    )
    store.record_call(
        run_id="run-1",
        iter=2,
        task_id="task-b",
        role="writer",
        model="claude-sonnet-4",
        usage=TokenUsage(**{"in": 1_000, "out": 500, "cached": 500}),
        latency_ms=200,
    )
    return store


# --------------------------------------------------------------------- record
def test_call_record_totals_and_cache_ratio() -> None:
    """``total_tokens`` is prompt + cached + output; ratio is cached / prompt."""
    record = CallRecord.from_usage(
        run_id="run-1",
        iter=3,
        task_id="task",
        role="writer",
        model="unknown-model",
        usage=TokenUsage(**{"in": 100, "out": 50, "cached": 50}),
        latency_ms=10,
    )

    assert record.total_tokens == 200
    assert record.prompt_tokens == 150
    assert record.cache_hit_ratio == pytest.approx(50 / 150)
    assert record.est_cost == 0.0, "unknown models must not invent a price"


# --------------------------------------------------------------------- queries
def test_tokens_by_role_and_iteration() -> None:
    """Totals are per role across the run and per iteration across roles."""
    store = store_with_calls()

    assert store.tokens_by_role() == {"writer": 3_700, "researcher": 1_000}
    assert store.tokens_by_role(iteration=1) == {"writer": 1_700, "researcher": 1_000}
    assert store.tokens_by_iteration() == {1: 2_700, 2: 2_000}
    assert store.total_tokens() == 4_700


def test_queries_can_be_scoped_to_a_run() -> None:
    """Two runs in one store never bleed into each other."""
    store = store_with_calls()
    store.record_call(
        run_id="run-2",
        iter=1,
        task_id="task-c",
        role="writer",
        model="claude-sonnet-4",
        usage=TokenUsage(**{"in": 10, "out": 5, "cached": 0}),
        latency_ms=1,
    )

    assert store.total_tokens("run-1") == 4_700
    assert store.total_tokens("run-2") == 15
    assert store.tokens_by_role(run_id="run-1") == {"writer": 3_700, "researcher": 1_000}
    assert store.records_for_run("run-2")[0].task_id == "task-c"


def test_cache_hit_ratio_is_cached_over_all_prompt_tokens() -> None:
    """Prompt = in + cached; 700 cached out of 3,500 prompt = 0.2."""
    store = store_with_calls()

    assert store.cache_hit_ratio() == pytest.approx(0.2)
    assert store.cache_hit_ratio(role="writer") == pytest.approx(700 / 2_700)
    assert store.cache_hit_ratio(role="researcher") == 0.0


def test_cache_hit_ratio_for_handles_no_prompt() -> None:
    """No prompt tokens means no ratio, not a division error."""
    record = CallRecord.from_usage(
        run_id="r",
        iter=0,
        task_id="t",
        role="writer",
        model="m",
        usage=TokenUsage(**{"in": 0, "out": 10, "cached": 0}),
        latency_ms=1,
    )
    assert cache_hit_ratio_for([record]) == 0.0


def test_cost_and_latency_aggregates() -> None:
    """Costs come from the price table; latency is a plain mean."""
    store = store_with_calls()

    assert store.total_cost() > 0, "sonnet pricing must be known"
    assert store.total_cost("missing-run") == 0.0
    assert store.avg_latency() == pytest.approx((100 + 300 + 200) / 3)
    assert store.avg_latency(role="researcher") == pytest.approx(300.0)


# ------------------------------------------------------------------------- ROI
def test_token_roi_is_quality_per_ten_thousand_tokens() -> None:
    """+10 Q over 20,000 tokens is 5 Q per 10k - the documented formula."""
    assert token_roi(60, 70, 20_000) == pytest.approx(5.0)
    assert token_roi(70, 60, 20_000) == pytest.approx(-5.0)
    assert token_roi(70, 70, 20_000) == pytest.approx(0.0)


def test_token_roi_without_spend_is_zero_not_undefined() -> None:
    """No tokens spent cannot produce a ratio; report 0.0 rather than inf."""
    assert token_roi(60, 70, 0) == 0.0


def test_token_roi_from_reports_uses_the_report_window() -> None:
    """First to last report, over the tokens spent between them."""
    reports = [report(1, 60), report(2, 65), report(3, 70)]

    assert token_roi_from_reports(reports, {1: 5_000, 2: 5_000, 3: 10_000}) == pytest.approx(5.0)
    assert token_roi_from_reports(reports[:1], {1: 5_000}) == 0.0
    assert token_roi_from_reports(reports, None) == 0.0


def test_store_roi_supports_both_calling_forms() -> None:
    """Reports or an explicit Q window both work against the run's tokens."""
    store = store_with_calls()

    assert store.roi("run-1", [report(1, 60), report(2, 70)]) == pytest.approx(
        10 / (4_700 / 10_000)
    )
    assert store.roi("run-1", q_start=60, q_end=70) == pytest.approx(10 / 0.47)
    assert store.roi("missing-run", q_start=60, q_end=70) == 0.0
    assert store.roi_for_window(60, 70, 20_000) == pytest.approx(5.0)


# --------------------------------------------------------------------- summary
def test_summary_is_the_evidence_payload() -> None:
    """One dict with counts, spend, cache ratio, and per-role/iteration splits."""
    summary = store_with_calls().summary("run-1")

    assert summary["call_count"] == 3
    assert summary["total_tokens"] == 4_700
    assert summary["tokens_by_role"] == {"writer": 3_700, "researcher": 1_000}
    assert summary["tokens_by_iteration"] == {"1": 2_700, "2": 2_000}
    assert summary["cache_hit_ratio"] == pytest.approx(0.2)


def test_clear_removes_one_run_or_everything() -> None:
    """Tests need a reset; production never calls this mid-run."""
    store = store_with_calls()
    store.clear("run-1")
    assert store.all_records() == []

    store.record_call(
        run_id="run-3",
        iter=0,
        task_id="t",
        role="writer",
        model="m",
        usage=TokenUsage(**{"in": 1, "out": 1}),
        latency_ms=1,
    )
    store.clear()
    assert store.all_records() == []


# ------------------------------------------------------------------ persistence
def test_records_survive_a_new_store_on_the_same_database(session_factory) -> None:
    """A restarted process can still report what a run spent."""
    first = TelemetryStore(session_factory=session_factory)
    first.record_call(
        run_id="run-persist",
        iter=1,
        task_id="task",
        role="writer",
        model="claude-sonnet-4",
        usage=TokenUsage(**{"in": 100, "out": 50, "cached": 25}),
        latency_ms=42,
    )

    restored = TelemetryStore(session_factory=session_factory)

    records = restored.all_records()
    assert len(records) == 1
    assert records[0].run_id == "run-persist"
    assert records[0].tokens_cached == 25
    assert restored.total_tokens("run-persist") == 175
