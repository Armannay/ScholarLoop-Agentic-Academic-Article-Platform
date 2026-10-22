"""Telemetry: per-call records, spend by role/iteration, cache ratio and token ROI.

The ROI test is the arithmetic contract the loop controller relies on: Q points gained
per 10k tokens, measured over the iterations that produced the gain.
"""

from __future__ import annotations

import pytest

from app.contracts.enums import BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMResponse
from app.governor import (
    CallRecord,
    TelemetryError,
    TelemetryRecorder,
    UsageTotals,
)
from tests.factories import sample_ulid

pytestmark = pytest.mark.governor


def response(
    *,
    model: str = "claude-sonnet-4",
    in_: int = 1_000,
    out: int = 200,
    cached: int = 0,
    latency_ms: int = 120,
) -> LLMResponse:
    """A provider answer with usage the recorder can bill."""
    return LLMResponse(
        text="{}",
        usage=TokenUsage(**{"in": in_, "out": out, "cached": cached}),
        model=model,
        latency_ms=latency_ms,
    )


def recorder(*, max_records: int = 100) -> TelemetryRecorder:
    return TelemetryRecorder(run_id="run-1", max_records=max_records)


# --------------------------------------------------------------------------- records
def test_record_from_response_bills_the_model_it_names():
    log = recorder()

    record = log.record(response(), iter=1, task_id=sample_ulid(3), role=BindingRole.WRITER)

    assert record.role == "writer"
    assert record.tokens == 1_200
    assert record.prompt_tokens == 1_000
    assert record.est_cost_usd > 0  # claude-sonnet-4 has a price row
    assert log.totals().calls == 1
    assert log.totals().tokens == 1_200


def test_unknown_model_costs_zero_but_still_counts_tokens():
    log = recorder()
    record = log.record(
        response(model="some-future-model"), iter=1, task_id=sample_ulid(3), role="writer"
    )

    assert record.est_cost_usd == 0.0
    assert log.totals().tokens == 1_200


def test_tokens_are_reported_by_role_and_iteration():
    log = recorder()
    log.record(response(in_=100, out=10), iter=1, task_id=sample_ulid(1), role="writer")
    log.record(response(in_=50, out=5), iter=1, task_id=sample_ulid(2), role="reviewer")
    log.record(response(in_=25, out=5), iter=2, task_id=sample_ulid(2), role="reviewer")

    by_role = log.tokens_by_role()
    by_iteration = log.tokens_by_iteration()

    assert by_role["writer"].tokens == 110
    assert by_role["reviewer"].tokens == 85
    assert by_role["reviewer"].calls == 2
    assert list(by_iteration) == [1, 2]
    assert by_iteration[1].tokens == 165
    assert by_iteration[2].tokens == 30


def test_cache_hit_ratio_overall_and_per_role():
    log = recorder()
    log.record(response(in_=250, out=0, cached=750), iter=1, task_id=sample_ulid(1), role="writer")
    log.record(response(in_=100, out=0, cached=0), iter=1, task_id=sample_ulid(1), role="validator")

    assert log.cache_hit_ratio() == pytest.approx(750 / 1_100)
    assert log.cache_hit_ratio(role="writer") == pytest.approx(0.75)
    assert log.cache_hit_ratio(role=BindingRole.VALIDATOR) == 0.0
    assert log.cache_hit_ratio(role="nobody") == 0.0


def test_estimate_drift_is_recorded_per_call_and_averaged():
    log = recorder()
    log.record(
        response(in_=1_000, out=0),
        iter=1,
        task_id=sample_ulid(1),
        role="writer",
        est_input_tokens=1_100,
    )
    log.record(
        response(in_=1_000, out=0),
        iter=2,
        task_id=sample_ulid(1),
        role="writer",
        est_input_tokens=900,
    )

    assert log.estimate_drift() == pytest.approx(0.0)
    assert log.records()[0].estimate_drift == pytest.approx(0.1)


def test_estimate_drift_is_none_without_estimates():
    log = recorder()
    log.record(response(), iter=1, task_id=sample_ulid(1), role="writer")

    assert log.estimate_drift() is None


def test_failed_calls_are_counted_as_failures():
    log = recorder()
    log.add(
        CallRecord(
            run_id="run-1",
            iter=1,
            task_id=sample_ulid(1),
            role="writer",
            model="claude-sonnet-4",
            ok=False,
        )
    )

    assert log.totals().failed_calls == 1


def test_mean_latency():
    log = recorder()
    log.record(response(latency_ms=100), iter=1, task_id=sample_ulid(1), role="writer")
    log.record(response(latency_ms=300), iter=1, task_id=sample_ulid(1), role="writer")

    assert log.totals().mean_latency_ms == pytest.approx(200.0)


# --------------------------------------------------------------------------- ROI
def test_token_roi_is_q_gained_per_ten_thousand_tokens():
    log = recorder()
    log.record_quality(1, 60.0)
    log.record_quality(3, 66.0)
    # Tokens spent in (1, 3]: 1,000 in iteration 2 and 1,000 in iteration 3.
    log.record(response(in_=900, out=100), iter=2, task_id=sample_ulid(1), role="writer")
    log.record(response(in_=900, out=100), iter=3, task_id=sample_ulid(2), role="reviewer")

    roi = log.token_roi()

    assert roi.q_before == 60.0
    assert roi.q_after == 66.0
    assert roi.q_gained == pytest.approx(6.0)
    assert roi.tokens == 2_000
    assert roi.iterations == 2
    assert roi.per_10k == pytest.approx(30.0)


def test_token_roi_can_be_restricted_to_an_iteration_window():
    log = recorder()
    for iteration, q in ((1, 50.0), (2, 55.0), (3, 56.0)):
        log.record_quality(iteration, q)
    log.record(response(in_=500, out=0), iter=2, task_id=sample_ulid(1), role="writer")
    log.record(response(in_=500, out=0), iter=3, task_id=sample_ulid(1), role="writer")

    roi = log.token_roi(iter_from=2)

    assert roi.q_before == 55.0
    assert roi.q_after == 56.0
    assert roi.tokens == 500  # only iteration 3 lies in (2, 3]
    assert roi.per_10k == pytest.approx(20.0)


def test_token_roi_needs_two_quality_samples():
    log = recorder()
    log.record_quality(1, 60.0)

    with pytest.raises(TelemetryError):
        log.token_roi()
    assert log.summary().roi is None


def test_quality_points_are_validated():
    log = recorder()
    with pytest.raises(ValueError, match=r"0\.\.100"):
        log.record_quality(1, 101.0)
    with pytest.raises(ValueError, match="iter"):
        log.record_quality(-1, 50.0)


def test_summary_combines_spend_and_roi():
    log = recorder()
    log.record_quality(1, 60.0)
    log.record_quality(2, 63.0)
    log.record(response(in_=1_000, out=0), iter=2, task_id=sample_ulid(1), role="writer")

    summary = log.summary()

    assert summary.run_id == "run-1"
    assert summary.tokens == 1_000
    assert summary.by_role["writer"].tokens == 1_000
    assert summary.roi is not None
    assert summary.roi.per_10k == pytest.approx(30.0)


# --------------------------------------------------------------------------- bounds
def test_detail_records_are_bounded_but_totals_are_not():
    log = recorder(max_records=2)
    for iteration in range(5):
        log.record(
            response(in_=10, out=0), iter=iteration, task_id=sample_ulid(iteration), role="writer"
        )

    assert len(log.records()) == 2
    assert log.totals().calls == 5
    assert log.totals().tokens == 50
    assert log.tokens_by_iteration()[0].calls == 1


def test_records_can_be_filtered():
    log = recorder()
    log.record(response(), iter=1, task_id=sample_ulid(1), role="writer")
    log.record(response(), iter=2, task_id=sample_ulid(1), role="reviewer")

    assert len(log.records_for(iter=1)) == 1
    assert len(log.records_for(role="reviewer")) == 1
    assert len(log.records_for(role="reviewer", iter=1)) == 0


def test_foreign_run_records_are_rejected():
    log = recorder()
    foreign = CallRecord(
        run_id="other-run", iter=0, task_id=sample_ulid(1), role="writer", model="m"
    )

    with pytest.raises(ValueError, match="other-run"):
        log.add(foreign)


def test_usage_totals_merge_is_additive():
    log = recorder()
    log.record(response(in_=100, out=10), iter=1, task_id=sample_ulid(1), role="writer")
    log.record(response(in_=100, out=10), iter=1, task_id=sample_ulid(1), role="writer")

    merged = UsageTotals().merge(log.tokens_by_role()["writer"])

    assert merged.calls == 2
    assert merged.tokens == 220
