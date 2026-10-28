"""Estimator behaviour: the 15 % margin, the provider override, and measured drift.

Every test runs against ``MockProvider`` or a tiny local stub - no network, no keys
(HARD RULE 5). The margin tests are arithmetic, not vibe: the expected value is
computed from the same ``prompt_tokens`` the estimator falls back to.
"""

from __future__ import annotations

import math

import pytest

from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.governor import BudgetEstimate, EstimateSource, TokenEstimate, TokenEstimator
from app.providers.base import prompt_tokens
from app.providers.errors import ProviderUnavailable
from app.providers.mock import MockProvider, Script
from tests.factories import make_request

pytestmark = pytest.mark.governor


class _DecliningProvider:
    """A provider whose tokeniser answers ``None`` (a legitimate answer)."""

    name = "declining"

    async def count_tokens(self, request: LLMRequest) -> int | None:
        return None


class _BrokenProvider:
    """A provider whose tokeniser raises: counting must never fail a call."""

    name = "broken"

    async def count_tokens(self, request: LLMRequest) -> int | None:
        raise ProviderUnavailable("tokeniser unavailable", provider="broken")


def usage(in_: int, out: int = 0, cached: int = 0) -> TokenUsage:
    return TokenUsage(**{"in": in_, "out": out, "cached": cached})


# --------------------------------------------------------------------------- margin
async def test_heuristic_applies_the_fifteen_percent_margin():
    estimator = TokenEstimator()
    request = make_request()
    raw = prompt_tokens(request)

    estimate = await estimator.estimate(request)

    assert estimate.source is EstimateSource.HEURISTIC
    assert estimate.raw_input_tokens == raw
    assert estimate.input_tokens == raw + math.ceil(raw * 0.15)
    assert estimate.safety_margin == pytest.approx(0.15)
    assert estimate.margin_ratio == pytest.approx(0.15, abs=0.02)


async def test_provider_tokeniser_wins_and_needs_no_margin(mock_provider: MockProvider):
    estimator = TokenEstimator(provider=mock_provider)
    request = make_request()

    estimate = await estimator.estimate(request)

    assert estimate.source is EstimateSource.PROVIDER
    assert estimate.input_tokens == prompt_tokens(request)
    assert estimate.margin_tokens == 0


async def test_provider_returning_none_falls_back_to_the_heuristic():
    estimator = TokenEstimator(provider=_DecliningProvider())  # type: ignore[arg-type]
    request = make_request()

    estimate = await estimator.estimate(request)

    assert estimate.source is EstimateSource.HEURISTIC
    assert estimate.input_tokens > estimate.raw_input_tokens


async def test_provider_error_falls_back_instead_of_failing_the_call():
    estimator = TokenEstimator(provider=_BrokenProvider())  # type: ignore[arg-type]

    estimate = await estimator.estimate(make_request())

    assert estimate.source is EstimateSource.HEURISTIC


async def test_margin_is_configurable_and_zero_means_raw():
    request = make_request()
    raw = prompt_tokens(request)

    no_margin = await TokenEstimator(safety_margin=0.0).estimate(request)
    wide_margin = await TokenEstimator(safety_margin=0.5).estimate(request)

    assert no_margin.input_tokens == raw
    assert wide_margin.input_tokens == raw + math.ceil(raw * 0.5)


async def test_estimate_for_budget_carries_the_output_ceiling():
    estimator = TokenEstimator()
    request = make_request()

    budget_estimate = await estimator.estimate_for_budget(request)

    assert isinstance(budget_estimate, BudgetEstimate)
    assert budget_estimate.output_tokens == request.max_output_tokens
    assert budget_estimate.source == EstimateSource.HEURISTIC.value


def test_estimator_rejects_impossible_configuration():
    with pytest.raises(ValueError, match="safety_margin"):
        TokenEstimator(safety_margin=-0.1)
    with pytest.raises(ValueError, match="window"):
        TokenEstimator(window=0)
    with pytest.raises(ValueError, match="smaller"):
        TokenEstimate(input_tokens=10, raw_input_tokens=20, source=EstimateSource.HEURISTIC)


# --------------------------------------------------------------------------- drift
def test_drift_report_bias_and_suggested_margin():
    estimator = TokenEstimator()
    estimator.record(1_150, 1_000)  # 15 % high
    estimator.record(900, 1_000)  # 10 % low

    report = estimator.report()

    assert report.samples == 2
    assert report.over_estimates == 1
    assert report.under_estimates == 1
    assert report.mean_relative_error == pytest.approx(0.025)
    assert report.mean_absolute_relative_error == pytest.approx(0.125)
    assert report.drift_ratio == pytest.approx(1.025)
    assert report.worst_under_estimate is not None
    assert report.worst_under_estimate.relative_error == pytest.approx(-0.1)
    assert not report.never_under_estimated


def test_suggested_margin_covers_a_consistent_under_estimate():
    estimator = TokenEstimator()
    for _ in range(3):
        estimator.record(900, 1_000)

    report = estimator.report()

    assert report.suggested_margin == pytest.approx(1 / 0.9 - 1, rel=1e-6)
    assert report.bias_ratio == pytest.approx(-0.1)


def test_suggested_margin_is_zero_when_the_estimator_never_under_counts():
    estimator = TokenEstimator()
    estimator.record(1_200, 1_000)

    report = estimator.report()

    assert report.suggested_margin == 0.0
    assert report.never_under_estimated


def test_reconcile_counts_prompt_plus_cached_tokens():
    estimator = TokenEstimator()

    sample = estimator.reconcile(1_100, usage(800, out=120, cached=200))

    assert sample.actual == 1_000
    assert sample.error == 100
    assert sample.source == "caller"  # a bare int estimate: the caller's own model


def test_window_bounds_memory_and_report():
    estimator = TokenEstimator(window=2)
    estimator.record(100, 100)
    estimator.record(200, 200)
    estimator.record(300, 300)

    report = estimator.report()

    assert report.samples == 2
    assert report.estimate_total == 500
    assert report.actual_total == 500


def test_empty_report_is_neutral():
    report = TokenEstimator().report()

    assert report.samples == 0
    assert report.drift_ratio == 0.0
    assert report.suggested_margin == 0.0
    assert report.never_under_estimated


def test_reset_clears_the_history():
    estimator = TokenEstimator()
    estimator.record(100, 50)
    estimator.reset()

    assert estimator.report().samples == 0


def test_drift_sample_rejects_negative_counts():
    estimator = TokenEstimator()
    with pytest.raises(ValueError, match=">= 0"):
        estimator.record(-1, 10)
    with pytest.raises(ValueError, match=">= 0"):
        estimator.record(10, -1)


async def test_provider_count_is_used_for_the_whole_prompt_including_schema():
    """A schema changes the prompt size, so the provider's count must see it."""
    estimator = TokenEstimator(provider=MockProvider(Script()))
    request = make_request(schema=True)

    estimate = await estimator.estimate(request)

    assert estimate.input_tokens == prompt_tokens(request)


def test_suggested_margin_covers_worst_sample_not_aggregate():
    estimator = TokenEstimator()
    estimator.record(1_500, 1_000)
    estimator.record(500, 1_000)
    report = estimator.report()
    assert report.drift_ratio == 1.0
    assert report.suggested_margin == pytest.approx(1.0)


def test_suggested_margin_for_zero_estimate_is_infinite():
    estimator = TokenEstimator()
    estimator.record(0, 100)
    assert math.isinf(estimator.report().suggested_margin)
