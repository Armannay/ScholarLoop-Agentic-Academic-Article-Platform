"""Token estimation: provider tokeniser first, chars/4 + 15 % margin otherwise.

The margin is the contract with the budget manager: an estimate must never
under-count by more than the documented slack, or ``precheck`` becomes theatre.
Drift reconciliation is what turns that promise into something measurable.
"""

from __future__ import annotations

import math

import pytest

from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMMessage, LLMRequest, SystemBlock
from app.governor.estimate import (
    CHARS_PER_TOKEN_FALLBACK,
    SAFETY_MARGIN,
    TokenEstimator,
    estimate_with_margin,
)
from app.providers.base import estimate_tokens as raw_estimate

pytestmark = pytest.mark.config


class CountingProvider:
    """A provider stub exposing ``count_tokens`` exactly like a real adapter."""

    def __init__(self, value: int | None = 1234, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises
        self.calls = 0

    async def count_tokens(self, request: LLMRequest) -> int | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("tokeniser unavailable")
        return self._value


def request_with(system: str = "s" * 400, user: str = "u" * 400) -> LLMRequest:
    """A request with predictable sizes."""
    return LLMRequest(
        system_blocks=[SystemBlock(text=system, cache=True)],
        messages=[LLMMessage(role="user", text=user)],
        max_output_tokens=256,
    )


# ------------------------------------------------------------------------- margin
def test_empty_text_estimates_to_zero() -> None:
    """No text, no tokens - the margin must not invent a cost."""
    assert estimate_with_margin("") == 0


def test_margin_is_fifteen_percent_above_chars_over_four() -> None:
    """ceil(chars/4) then +15 %, rounded up: 400 chars -> 115 tokens."""
    assert CHARS_PER_TOKEN_FALLBACK == 4
    assert pytest.approx(1.15) == SAFETY_MARGIN
    assert estimate_with_margin("a" * 400) == 115
    assert estimate_with_margin("a" * 400) == math.ceil(raw_estimate("a" * 400) * 1.15)


def test_short_text_still_costs_at_least_one_margined_token() -> None:
    """A one-character prompt is 2 tokens once the margin is applied."""
    assert estimate_with_margin("a") == 2
    assert estimate_with_margin("ab") == 2


@pytest.mark.parametrize("length", [37, 100, 999, 4_000, 12_345])
def test_estimate_always_covers_the_raw_estimate_plus_margin(length: int) -> None:
    """The estimate never falls below raw x 1.15 (that is the whole point)."""
    text = "x" * length
    estimate = estimate_with_margin(text)
    assert estimate >= math.ceil(raw_estimate(text) * SAFETY_MARGIN)


def test_request_estimate_counts_system_messages_and_overhead() -> None:
    """System 400 chars (115) + user 400 chars (115 + 4 overhead) = 234."""
    estimator = TokenEstimator()
    assert estimator.estimate_sync(request_with()) == 234


# ------------------------------------------------------------------- provider use
async def test_provider_count_tokens_is_used_verbatim() -> None:
    """When the provider can count, its number wins over any heuristic."""
    provider = CountingProvider(value=1_234)
    estimator = TokenEstimator(provider)

    assert await estimator.estimate(request_with()) == 1_234
    assert provider.calls == 1


async def test_provider_returning_none_falls_back_to_the_margin_estimate() -> None:
    """``None`` is a legitimate answer: fall back, do not fail."""
    estimator = TokenEstimator(CountingProvider(value=None))

    assert await estimator.estimate(request_with()) == estimator.estimate_sync(request_with())


async def test_provider_tokeniser_failure_falls_back_quietly() -> None:
    """A broken tokeniser must not break budgeting."""
    estimator = TokenEstimator(CountingProvider(raises=True))

    assert await estimator.estimate(request_with()) == estimator.estimate_sync(request_with())


async def test_estimator_without_a_provider_uses_the_sync_path() -> None:
    """No provider, no I/O, same deterministic answer."""
    estimator = TokenEstimator()
    request = request_with()

    assert await estimator.estimate(request) == estimator.estimate_sync(request)


# ---------------------------------------------------------------- reconciliation
def test_reconcile_reports_under_estimate_drift() -> None:
    """Actual 120 vs estimate 100 is +20 % drift, recorded as under-estimated."""
    estimator = TokenEstimator()
    record = estimator.reconcile(100, TokenUsage(**{"in": 80, "out": 40, "cached": 0}))

    assert record.estimate == 100
    assert record.actual == 120
    assert record.delta == 20
    assert record.drift_ratio == pytest.approx(0.20)
    assert record.error_pct == pytest.approx(20.0)
    assert record.over_estimated is False


def test_reconcile_reports_over_estimate_drift() -> None:
    """Actual 80 vs estimate 100 is a safe over-estimate."""
    estimator = TokenEstimator()
    record = estimator.reconcile(100, 80)

    assert record.delta == -20
    assert record.over_estimated is True
    assert estimator.drift_stats.max_over_estimate == 20


def test_drift_stats_aggregate_every_reconciliation() -> None:
    """Means, totals and the direction counters all move together."""
    estimator = TokenEstimator()
    estimator.reconcile(100, 120)  # under by 20
    estimator.reconcile(100, 90)  # over by 10

    stats = estimator.drift_stats
    assert stats.count == 2
    assert stats.total_estimate == 200
    assert stats.total_actual == 210
    assert stats.mean_drift_ratio == pytest.approx((0.20 + -0.10) / 2)
    assert stats.mean_abs_error_pct == pytest.approx((20.0 + 10.0) / 2)
    assert stats.max_under_estimate == 20
    assert stats.max_over_estimate == 10
    assert stats.total_under_estimates == 1
    assert stats.total_over_estimates == 1
    assert stats.overall_drift_ratio == pytest.approx(0.05)


def test_reconcile_request_estimates_then_records() -> None:
    """The convenience wrapper estimates the request itself."""
    estimator = TokenEstimator()
    request = request_with()

    record = estimator.reconcile_request(request, TokenUsage(**{"in": 200, "out": 100}))

    assert record.estimate == estimator.estimate_sync(request)
    assert record.actual == 300


def test_reset_clears_history_but_not_estimation() -> None:
    """Between runs the drift history restarts; the estimator still works."""
    estimator = TokenEstimator()
    estimator.reconcile(10, 12)
    estimator.reset()

    assert estimator.drift_stats.count == 0
    assert estimator.estimate_text("a" * 400) == 115


def test_summary_is_json_friendly() -> None:
    """Telemetry logs need plain numbers, not a dataclass."""
    estimator = TokenEstimator()
    estimator.reconcile(100, 120)
    summary = estimator.summary()

    assert summary["count"] == 1
    assert summary["total_actual"] == 120
    assert isinstance(summary["mean_drift_ratio"], float)
