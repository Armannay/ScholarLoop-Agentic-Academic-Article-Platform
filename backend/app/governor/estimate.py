"""Token estimation with provider-aware sizing and drift tracking.

Governor rule: the *cheapest* trustworthy number wins.

* If the provider exposes ``count_tokens`` (``LLMProvider.count_tokens``) we use
  that value verbatim - the vendor's tokeniser is ground truth for the call
  about to be made.
* Otherwise we estimate as ``ceil(chars / 4)`` (the platform-wide ``CHARS_PER_TOKEN``
  convention) and add a 15 % safety margin, so an estimate is biased *above* the
  likely prompt size rather than below it. The bias is not a guarantee: every
  estimate is reconciled against real usage and the drift is reported.

Every estimate can be reconciled against the real :class:`TokenUsage` returned
by the provider; the drift history is exposed for tuning and for the "diminishing
returns" erosion detector.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any

from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.providers.base import CHARS_PER_TOKEN, MESSAGE_OVERHEAD_TOKENS, prompt_tokens
from app.providers.base import estimate_tokens as base_estimate

logger = logging.getLogger(__name__)

__all__ = [
    "CHARS_PER_TOKEN_FALLBACK",
    "SAFETY_MARGIN",
    "DriftRecord",
    "DriftStats",
    "TokenEstimator",
    "estimate_with_margin",
]

CHARS_PER_TOKEN_FALLBACK: int = CHARS_PER_TOKEN  # 4, the documented approximation
SAFETY_MARGIN: float = 1.15  # 15 % above the chars/4 estimate


def estimate_with_margin(text: str) -> int:
    """Chars/4 estimate with the 15 % safety margin baked in.

    Empty text is 0. Otherwise ``ceil(ceil(len(text) / 4) * 1.15)``, at least 1.
    Deterministic and synchronous: safe to call from any thread, no I/O.
    """
    if not text:
        return 0
    raw = math.ceil(len(text) / CHARS_PER_TOKEN_FALLBACK)
    margined = math.ceil(raw * SAFETY_MARGIN)
    return max(1, margined)


def estimate_request_with_margin(request: LLMRequest) -> int:
    """Estimate a whole request with margin: system + messages + overhead."""
    system = sum(estimate_with_margin(block.text) for block in request.system_blocks)
    messages = sum(
        estimate_with_margin(message.text) + MESSAGE_OVERHEAD_TOKENS for message in request.messages
    )
    return system + messages


@dataclass(frozen=True)
class DriftRecord:
    """One reconciliation: estimate vs reality."""

    estimate: int
    actual: int
    delta: int
    drift_ratio: float  # (actual - estimate) / estimate, 0 when estimate==0
    over_estimated: bool
    error_pct: float  # abs(actual-estimate)/max(1, estimate) * 100


@dataclass
class DriftStats:
    """Aggregated drift. All fields are derived, never set directly."""

    count: int = 0
    mean_drift_ratio: float = 0.0
    mean_abs_error_pct: float = 0.0
    max_over_estimate: int = 0
    max_under_estimate: int = 0
    total_estimate: int = 0
    total_actual: int = 0
    total_over_estimates: int = 0
    total_under_estimates: int = 0

    @property
    def overall_drift_ratio(self) -> float:
        """Total actual vs total estimate across all reconciliations."""
        if self.total_estimate == 0:
            return 0.0
        return (self.total_actual - self.total_estimate) / self.total_estimate


class TokenEstimator:
    """Estimate, reconcile and track drift for one provider.

    Args:
        provider: optional ``LLMProvider``. When ``None`` the estimator always
            uses the chars/4 + 15 % margin path. When a provider is present we
            call ``provider.count_tokens`` and fall back to the margin estimate
            only when the provider returns ``None``.

    The class is thread-safe: ``estimate`` may be ``async`` while
    ``reconcile`` is called from the record path.
    """

    def __init__(self, provider: Any | None = None) -> None:
        self._provider: Any | None = provider
        self._lock = threading.Lock()
        self._records: list[DriftRecord] = []
        self._stats = DriftStats()

    # -- estimation --------------------------------------------------------
    def estimate_sync(self, request: LLMRequest) -> int:
        """Synchronous fallback estimate: chars/4 + 15 % margin."""
        return estimate_request_with_margin(request)

    async def estimate(self, request: LLMRequest) -> int:
        """Best estimate for ``request`` (async to allow ``count_tokens``).

        Tries ``provider.count_tokens`` first; when unavailable or ``None``
        falls back to :func:`estimate_request_with_margin`.
        """
        if self._provider is not None:
            count_fn = getattr(self._provider, "count_tokens", None)
            if callable(count_fn):
                try:
                    value: int | None = await count_fn(request)
                    if isinstance(value, int) and value >= 0:
                        return value
                except Exception as exc:
                    # Vendor tokeniser failures must not break budgeting.
                    logger.debug("provider.count_tokens failed (%s); using estimate", exc)
        return self.estimate_sync(request)

    def estimate_text(self, text: str) -> int:
        """Estimate for a single string with margin."""
        return estimate_with_margin(text)

    def estimate_tokens_raw(self, text: str) -> int:
        """Raw chars/4 estimate without margin (the base convention)."""
        return base_estimate(text)

    def prompt_tokens_raw(self, request: LLMRequest) -> int:
        """Raw prompt estimate without margin (for comparison)."""
        return prompt_tokens(request)

    # -- reconciliation ----------------------------------------------------
    def reconcile(self, estimate: int, actual: TokenUsage | int) -> DriftRecord:
        """Record ``estimate`` vs ``actual`` and return the drift.

        Args:
            estimate: the value previously returned by :meth:`estimate`.
            actual: the provider's real usage (``TokenUsage.total`` or raw int).

        Updates :attr:`drift_stats` atomically.
        """
        actual_total = actual.total if isinstance(actual, TokenUsage) else int(actual)
        delta = actual_total - estimate
        drift_ratio = (delta / estimate) if estimate else 0.0
        error_pct = (abs(delta) / max(1, estimate) * 100.0) if estimate else 0.0
        record = DriftRecord(
            estimate=estimate,
            actual=actual_total,
            delta=delta,
            drift_ratio=drift_ratio,
            over_estimated=estimate >= actual_total,
            error_pct=error_pct,
        )
        with self._lock:
            self._records.append(record)
            self._recompute_stats_locked()
        return record

    def reconcile_request(self, request: LLMRequest, actual: TokenUsage | int) -> DriftRecord:
        """Convenience: estimate the request synchronously then reconcile."""
        estimate = self.estimate_sync(request)
        return self.reconcile(estimate, actual)

    # -- stats -------------------------------------------------------------
    @property
    def drift_stats(self) -> DriftStats:
        with self._lock:
            # Return a copy so callers cannot mutate internal state.
            return DriftStats(
                count=self._stats.count,
                mean_drift_ratio=self._stats.mean_drift_ratio,
                mean_abs_error_pct=self._stats.mean_abs_error_pct,
                max_over_estimate=self._stats.max_over_estimate,
                max_under_estimate=self._stats.max_under_estimate,
                total_estimate=self._stats.total_estimate,
                total_actual=self._stats.total_actual,
                total_over_estimates=self._stats.total_over_estimates,
                total_under_estimates=self._stats.total_under_estimates,
            )

    @property
    def mean_drift(self) -> float:
        return self.drift_stats.mean_drift_ratio

    @property
    def records(self) -> list[DriftRecord]:
        with self._lock:
            return list(self._records)

    @property
    def drift(self) -> float:
        """Alias for mean drift ratio (positive = under-estimated on average)."""
        return self.mean_drift

    def _recompute_stats_locked(self) -> None:
        # Caller holds self._lock
        if not self._records:
            self._stats = DriftStats()
            return
        total_estimate = sum(r.estimate for r in self._records)
        total_actual = sum(r.actual for r in self._records)
        mean_drift = sum(r.drift_ratio for r in self._records) / len(self._records)
        mean_err = sum(r.error_pct for r in self._records) / len(self._records)
        # max_over_estimate = largest positive (estimate - actual)
        over = max((r.estimate - r.actual for r in self._records if r.over_estimated), default=0)
        under = max(
            (r.actual - r.estimate for r in self._records if not r.over_estimated), default=0
        )
        over_count = sum(1 for r in self._records if r.over_estimated)
        under_count = len(self._records) - over_count
        self._stats = DriftStats(
            count=len(self._records),
            mean_drift_ratio=mean_drift,
            mean_abs_error_pct=mean_err,
            max_over_estimate=over,
            max_under_estimate=under,
            total_estimate=total_estimate,
            total_actual=total_actual,
            total_over_estimates=over_count,
            total_under_estimates=under_count,
        )

    # -- helpers -----------------------------------------------------------
    def reset(self) -> None:
        """Clear drift history (between runs)."""
        with self._lock:
            self._records.clear()
            self._stats = DriftStats()

    def summary(self) -> dict[str, Any]:
        """JSON-serialisable summary for logs/telemetry."""
        s = self.drift_stats
        return {
            "count": s.count,
            "mean_drift_ratio": round(s.mean_drift_ratio, 4),
            "mean_abs_error_pct": round(s.mean_abs_error_pct, 2),
            "overall_drift_ratio": round(s.overall_drift_ratio, 4),
            "max_over_estimate": s.max_over_estimate,
            "max_under_estimate": s.max_under_estimate,
            "total_estimate": s.total_estimate,
            "total_actual": s.total_actual,
        }
