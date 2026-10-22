"""Token estimation with an explicit safety margin, plus measured drift.

Two questions, two answers:

* **How big is this call going to be?** :meth:`TokenEstimator.estimate` asks the
  provider's tokeniser when it has one (``LLMProvider.count_tokens``) and otherwise
  falls back to the project-wide ``chars / 4`` convention *plus* a 15 % safety margin.
  The margin exists because ``chars / 4`` under-counts code, JSON and non-Latin text -
  and an under-count is the one error a budget cannot absorb.
* **Was the estimate any good?** :meth:`TokenEstimator.reconcile` compares the estimate
  with the provider's reported prompt usage and :meth:`TokenEstimator.report` exposes
  the drift: signed bias, mean absolute error, and the suggested margin that would have
  covered every sample. The loop controller logs this; a repeatedly under-estimating
  estimator is a policy bug, not a rounding detail.

Nothing here calls the network: ``count_tokens`` is a provider *method* and the mock
implements it exactly (HARD RULE 5). A provider error while counting is not fatal -
the estimate falls back to the heuristic and says so via ``EstimateSource``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.core.logging import get_logger
from app.governor.budget import BudgetEstimate
from app.providers.base import LLMProvider, estimate_tokens, prompt_tokens
from app.providers.errors import ProviderError

__all__ = [
    "DEFAULT_SAFETY_MARGIN",
    "DEFAULT_WINDOW",
    "DriftReport",
    "DriftSample",
    "EstimateSource",
    "TokenEstimate",
    "TokenEstimator",
]

logger = get_logger(__name__)

#: Applied on top of the heuristic. 15 % is the documented default; it is a policy
#: number, so it is named here rather than buried in a call site.
DEFAULT_SAFETY_MARGIN: Final[float] = 0.15
#: Drift samples kept for the report. Bounded on purpose: telemetry must not grow
#: without limit inside a long run.
DEFAULT_WINDOW: Final[int] = 256


class EstimateSource(StrEnum):
    """Where an estimate came from - ``provider`` counts are trusted as-is."""

    PROVIDER = "provider"
    HEURISTIC = "heuristic"


@dataclass(frozen=True)
class TokenEstimate:
    """One prompt-size estimate, with the margin that was applied spelled out."""

    input_tokens: int
    raw_input_tokens: int
    source: EstimateSource
    safety_margin: float = 0.0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.raw_input_tokens < 0:
            raise ValueError("token estimates must be >= 0")
        if self.safety_margin < 0.0:
            raise ValueError("safety_margin must be >= 0.0")
        if self.input_tokens < self.raw_input_tokens:
            raise ValueError("the margined estimate cannot be smaller than the raw one")

    @property
    def margin_tokens(self) -> int:
        """Head-room the margin added, in tokens."""
        return self.input_tokens - self.raw_input_tokens

    @property
    def margin_ratio(self) -> float:
        """Head-room relative to the raw estimate, ``0.0`` when raw is 0."""
        if self.raw_input_tokens == 0:
            return 0.0
        return self.margin_tokens / self.raw_input_tokens

    def to_budget(self, *, output_tokens: int = 0, cost_usd: float = 0.0) -> BudgetEstimate:
        """The :class:`~app.governor.budget.BudgetEstimate` a governor can precheck."""
        return BudgetEstimate(
            input_tokens=self.input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            source=self.source.value,
        )


@dataclass(frozen=True)
class DriftSample:
    """One estimate paired with the prompt tokens the provider actually billed."""

    estimate: int
    actual: int
    source: str = EstimateSource.HEURISTIC.value

    def __post_init__(self) -> None:
        if self.estimate < 0 or self.actual < 0:
            raise ValueError("drift samples must be >= 0")

    @property
    def error(self) -> int:
        """Signed error: positive means the estimate was high (safe)."""
        return self.estimate - self.actual

    @property
    def relative_error(self) -> float:
        """Signed error relative to the actual count; ``0.0`` when both are empty."""
        if self.actual == 0:
            return 0.0 if self.estimate == 0 else math.inf
        return self.error / self.actual

    @property
    def over_estimate(self) -> bool:
        return self.error > 0

    @property
    def under_estimate(self) -> bool:
        return self.error < 0


@dataclass(frozen=True)
class DriftReport:
    """Aggregate quality of the estimator over the retained window."""

    samples: int
    estimate_total: int
    actual_total: int
    mean_relative_error: float
    mean_absolute_relative_error: float
    over_estimates: int
    under_estimates: int
    worst_under_estimate: DriftSample | None = None

    @property
    def drift_ratio(self) -> float:
        """Estimate / actual over the window; ``0.0`` when nothing was measured."""
        if self.actual_total == 0:
            return 0.0
        return self.estimate_total / self.actual_total

    @property
    def bias_ratio(self) -> float:
        """How far the estimate sits above actual (0.05 = 5 % high, -0.05 = 5 % low)."""
        return self.drift_ratio - 1.0 if self.actual_total else 0.0

    @property
    def suggested_margin(self) -> float:
        """Smallest uniform margin that would have covered every sample in the window."""
        return max(0.0, 1.0 / self.drift_ratio - 1.0) if self.drift_ratio else 0.0

    @property
    def never_under_estimated(self) -> bool:
        """True when no sample under-counted the prompt."""
        return self.worst_under_estimate is None


class TokenEstimator:
    """Estimates prompt size, records the drift and reports the correction.

    Args:
        provider: optional provider used for ``count_tokens``; a per-call ``provider=``
            argument overrides it.
        safety_margin: margin applied to the heuristic fallback only.
        window: number of drift samples retained for the report.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        safety_margin: float = DEFAULT_SAFETY_MARGIN,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        if safety_margin < 0.0:
            raise ValueError(f"safety_margin must be >= 0.0, got {safety_margin}")
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window}")
        self._provider = provider
        self._safety_margin = safety_margin
        self._samples: deque[DriftSample] = deque(maxlen=window)

    @property
    def safety_margin(self) -> float:
        """Margin applied to heuristic estimates."""
        return self._safety_margin

    async def estimate(
        self,
        request: LLMRequest,
        *,
        provider: LLMProvider | None = None,
    ) -> TokenEstimate:
        """Estimate the prompt size of ``request``.

        The provider's tokeniser wins when it answers; a provider that returns ``None``,
        is absent, or raises :class:`~app.providers.errors.ProviderError` falls back to
        the heuristic. Counting must never be the reason a call fails.
        """
        chosen = provider if provider is not None else self._provider
        exact = await self._count_tokens(chosen, request)
        if exact is not None:
            return TokenEstimate(
                input_tokens=exact,
                raw_input_tokens=exact,
                source=EstimateSource.PROVIDER,
            )
        raw = prompt_tokens(request)
        margined = raw + math.ceil(raw * self._safety_margin)
        return TokenEstimate(
            input_tokens=margined,
            raw_input_tokens=raw,
            source=EstimateSource.HEURISTIC,
            safety_margin=self._safety_margin,
        )

    async def estimate_for_budget(
        self,
        request: LLMRequest,
        *,
        provider: LLMProvider | None = None,
        cost_usd: float = 0.0,
    ) -> BudgetEstimate:
        """Convenience: estimate and shape it for :meth:`BudgetGovernor.precheck`."""
        estimate = await self.estimate(request, provider=provider)
        return estimate.to_budget(output_tokens=request.max_output_tokens, cost_usd=cost_usd)

    def record(self, estimate: TokenEstimate | int, actual_input_tokens: int) -> DriftSample:
        """Remember one (estimate, actual) pair and return it.

        Estimate may be a :class:`TokenEstimate` or a bare token count (a caller that
        predicted with its own model).
        """
        predicted = estimate.input_tokens if isinstance(estimate, TokenEstimate) else estimate
        source = estimate.source.value if isinstance(estimate, TokenEstimate) else "caller"
        sample = DriftSample(estimate=predicted, actual=actual_input_tokens, source=source)
        self._samples.append(sample)
        return sample

    def reconcile(self, estimate: TokenEstimate | int, usage: TokenUsage) -> DriftSample:
        """Compare an estimate with the prompt usage the provider reported.

        ``usage.in_ + usage.cached`` is the whole prompt: cached tokens are still prompt
        tokens, they are just billed at a lower rate.
        """
        return self.record(estimate, usage.in_ + usage.cached)

    def report(self) -> DriftReport:
        """Drift over the retained window (bounded, so a long run cannot bloat memory)."""
        if not self._samples:
            return DriftReport(
                samples=0,
                estimate_total=0,
                actual_total=0,
                mean_relative_error=0.0,
                mean_absolute_relative_error=0.0,
                over_estimates=0,
                under_estimates=0,
            )
        samples = list(self._samples)
        finite = [
            sample.relative_error for sample in samples if math.isfinite(sample.relative_error)
        ]
        mean_relative = sum(finite) / len(finite) if finite else 0.0
        mean_absolute = sum(abs(value) for value in finite) / len(finite) if finite else 0.0
        worst = min(samples, key=lambda sample: sample.relative_error)
        return DriftReport(
            samples=len(samples),
            estimate_total=sum(sample.estimate for sample in samples),
            actual_total=sum(sample.actual for sample in samples),
            mean_relative_error=mean_relative,
            mean_absolute_relative_error=mean_absolute,
            over_estimates=sum(1 for sample in samples if sample.over_estimate),
            under_estimates=sum(1 for sample in samples if sample.under_estimate),
            worst_under_estimate=worst if worst.under_estimate else None,
        )

    def reset(self) -> None:
        """Forget every sample (a new run starts a new drift history)."""
        self._samples.clear()

    @staticmethod
    async def _count_tokens(provider: LLMProvider | None, request: LLMRequest) -> int | None:
        """Ask a provider for an exact count, tolerating every way it can decline."""
        if provider is None:
            return None
        counter = getattr(provider, "count_tokens", None)
        if counter is None:
            return None
        try:
            value = await counter(request)
        except (ProviderError, NotImplementedError) as exc:
            logger.info("count_tokens unavailable (%s); using the heuristic", exc)
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value


def estimate_text_tokens(text: str) -> int:
    """The project-wide heuristic for a bare string, re-exported for symmetry."""
    return estimate_tokens(text)
