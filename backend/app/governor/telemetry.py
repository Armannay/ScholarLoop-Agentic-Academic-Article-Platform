"""Per-call telemetry and the questions the loop controller actually asks.

One record per provider call - run, iteration, task, role, model, tokens in/out/cached,
latency, estimated cost, and the estimate it replaced - and four queries on top of it:

* **tokens by role** - which agent is eating the budget;
* **tokens by iteration** - whether the loop is spending more for the same result;
* **cache-hit ratio** - whether the stable prefix is actually being cached;
* **token ROI** - Q points gained per 10,000 tokens, the number that decides whether
  another iteration is worth its spend.

Memory is bounded on purpose: the detail list keeps the newest ``max_records`` calls
while the aggregates (totals, per-role, per-iteration) continue to count everything, so
a long run reports correct totals without growing without limit. Cost is the best-effort
figure from :func:`app.core.pricing.estimate_cost`; the token budgets are the authority.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.contracts.enums import BindingRole
from app.contracts.llm import LLMResponse
from app.core.clock import utc_now
from app.core.errors import ScholarLoopError
from app.core.pricing import estimate_cost

__all__ = [
    "DEFAULT_MAX_RECORDS",
    "CallRecord",
    "QualityPoint",
    "TelemetryRecorder",
    "TelemetrySummary",
    "TokenROI",
    "UsageTotals",
]

#: Detail records kept in memory. Older calls still count towards the aggregates.
DEFAULT_MAX_RECORDS: Final[int] = 10_000


class TelemetryError(ScholarLoopError):
    """A query needs data that has not been recorded yet."""

    code = "telemetry_insufficient_data"
    http_status = 500


@dataclass(frozen=True)
class CallRecord:
    """One provider call, as the telemetry log wants it."""

    run_id: str
    iter: int
    task_id: str
    role: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    latency_ms: int = 0
    est_cost_usd: float = 0.0
    est_input_tokens: int | None = None
    ok: bool = True
    ts: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.iter < 0:
            raise ValueError("iter must be >= 0")
        for name in ("tokens_in", "tokens_out", "tokens_cached", "latency_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.est_cost_usd < 0.0:
            raise ValueError("est_cost_usd must be >= 0.0")

    @classmethod
    def from_response(
        cls,
        response: LLMResponse,
        *,
        run_id: str,
        iter: int,
        task_id: str,
        role: BindingRole | str,
        latency_ms: int | None = None,
        est_input_tokens: int | None = None,
        ts: datetime | None = None,
    ) -> CallRecord:
        """Build a record from a provider answer, pricing it by the model it names."""
        usage = response.usage
        return cls(
            run_id=run_id,
            iter=iter,
            task_id=task_id,
            role=role.value if isinstance(role, BindingRole) else str(role),
            model=response.model,
            tokens_in=usage.in_,
            tokens_out=usage.out,
            tokens_cached=usage.cached,
            latency_ms=response.latency_ms if latency_ms is None else latency_ms,
            est_cost_usd=estimate_cost(response.model, usage),
            est_input_tokens=est_input_tokens,
            ts=ts if ts is not None else utc_now(),
        )

    @property
    def tokens(self) -> int:
        """All tokens billed for this call."""
        return self.tokens_in + self.tokens_cached + self.tokens_out

    @property
    def prompt_tokens(self) -> int:
        """Prompt tokens: full-rate plus cached (a provider reports them separately)."""
        return self.tokens_in + self.tokens_cached

    @property
    def cache_hit_ratio(self) -> float:
        """Share of the prompt served from the cache, ``0.0`` when there was none."""
        prompt = self.prompt_tokens
        return 0.0 if prompt == 0 else self.tokens_cached / prompt

    @property
    def estimate_drift(self) -> float | None:
        """Signed relative error of the pre-call estimate, ``None`` when not recorded."""
        if self.est_input_tokens is None or self.prompt_tokens == 0:
            return None
        return (self.est_input_tokens - self.prompt_tokens) / self.prompt_tokens


@dataclass(frozen=True)
class UsageTotals:
    """Additive totals for a role, an iteration or a whole run."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    est_cost_usd: float = 0.0
    latency_ms: int = 0
    failed_calls: int = 0

    @property
    def tokens(self) -> int:
        """All tokens, matching :attr:`CallRecord.tokens`."""
        return self.tokens_in + self.tokens_cached + self.tokens_out

    @property
    def prompt_tokens(self) -> int:
        return self.tokens_in + self.tokens_cached

    @property
    def cache_hit_ratio(self) -> float:
        prompt = self.prompt_tokens
        return 0.0 if prompt == 0 else self.tokens_cached / prompt

    @property
    def mean_latency_ms(self) -> float:
        """Average latency per call, ``0.0`` when nothing was recorded."""
        return 0.0 if self.calls == 0 else self.latency_ms / self.calls

    def plus(self, record: CallRecord) -> UsageTotals:
        """This total plus one call (records are immutable; totals are replaced)."""
        return UsageTotals(
            calls=self.calls + 1,
            tokens_in=self.tokens_in + record.tokens_in,
            tokens_out=self.tokens_out + record.tokens_out,
            tokens_cached=self.tokens_cached + record.tokens_cached,
            est_cost_usd=self.est_cost_usd + record.est_cost_usd,
            latency_ms=self.latency_ms + record.latency_ms,
            failed_calls=self.failed_calls + (0 if record.ok else 1),
        )

    def merge(self, other: UsageTotals) -> UsageTotals:
        """Sum of two totals."""
        return UsageTotals(
            calls=self.calls + other.calls,
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            tokens_cached=self.tokens_cached + other.tokens_cached,
            est_cost_usd=self.est_cost_usd + other.est_cost_usd,
            latency_ms=self.latency_ms + other.latency_ms,
            failed_calls=self.failed_calls + other.failed_calls,
        )


@dataclass(frozen=True)
class QualityPoint:
    """Q measured at one iteration. The loop controller supplies these."""

    iter: int
    q: float

    def __post_init__(self) -> None:
        if self.iter < 0:
            raise ValueError("iter must be >= 0")
        if not 0.0 <= self.q <= 100.0:
            raise ValueError(f"Q must be in 0..100, got {self.q}")


@dataclass(frozen=True)
class TokenROI:
    """Q gained per 10,000 tokens between two measurement points."""

    q_before: float
    q_after: float
    tokens: int
    iterations: int

    @property
    def q_gained(self) -> float:
        return self.q_after - self.q_before

    @property
    def per_10k(self) -> float:
        """The ROI itself: Q points per 10k tokens, ``0.0`` when nothing was spent."""
        if self.tokens <= 0:
            return 0.0
        return self.q_gained / self.tokens * 10_000


@dataclass(frozen=True)
class TelemetrySummary:
    """Everything a decision needs to see about spend, in one object."""

    run_id: str
    totals: UsageTotals
    by_role: dict[str, UsageTotals]
    by_iteration: dict[int, UsageTotals]
    cache_hit_ratio: float
    roi: TokenROI | None
    estimate_drift: float | None

    @property
    def tokens(self) -> int:
        return self.totals.tokens

    @property
    def est_cost_usd(self) -> float:
        return self.totals.est_cost_usd


class TelemetryRecorder:
    """Bounded per-call log with role/iteration/ROI queries.

    Args:
        run_id: the run these calls belong to; every record carries it.
        max_records: detail records kept in memory; aggregates always cover all calls.
    """

    def __init__(self, *, run_id: str, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if max_records < 1:
            raise ValueError(f"max_records must be >= 1, got {max_records}")
        self._run_id = run_id
        self._records: deque[CallRecord] = deque(maxlen=max_records)
        self._totals = UsageTotals()
        self._by_role: dict[str, UsageTotals] = {}
        self._by_iteration: dict[int, UsageTotals] = {}
        self._quality: dict[int, float] = {}
        self._drift_sum = 0.0
        self._drift_samples = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    # -- writing -----------------------------------------------------------
    def record(
        self,
        response: LLMResponse,
        *,
        iter: int,
        task_id: str,
        role: BindingRole | str,
        est_input_tokens: int | None = None,
        latency_ms: int | None = None,
        ts: datetime | None = None,
    ) -> CallRecord:
        """Record one successful provider call and return the stored record."""
        record = CallRecord.from_response(
            response,
            run_id=self._run_id,
            iter=iter,
            task_id=task_id,
            role=role,
            est_input_tokens=est_input_tokens,
            latency_ms=latency_ms,
            ts=ts,
        )
        self.add(record)
        return record

    def add(self, record: CallRecord) -> None:
        """Store an already-built record (failures included, with ``ok=False``)."""
        if record.run_id != self._run_id:
            raise ValueError(
                f"record belongs to run {record.run_id!r}, recorder tracks {self._run_id!r}"
            )
        self._records.append(record)
        self._totals = self._totals.plus(record)
        self._by_role[record.role] = self._by_role.get(record.role, UsageTotals()).plus(record)
        self._by_iteration[record.iter] = self._by_iteration.get(record.iter, UsageTotals()).plus(
            record
        )
        drift = record.estimate_drift
        if drift is not None and math.isfinite(drift):
            self._drift_sum += drift
            self._drift_samples += 1

    def record_quality(self, iter: int, q: float) -> QualityPoint:
        """Remember the Q measured at ``iter``; later reports reuse it for ROI."""
        point = QualityPoint(iter=iter, q=q)
        self._quality[point.iter] = point.q
        return point

    # -- reading -----------------------------------------------------------
    def records(self) -> tuple[CallRecord, ...]:
        """Retained detail records, oldest first."""
        return tuple(self._records)

    def records_for(
        self, *, role: BindingRole | str | None = None, iter: int | None = None
    ) -> tuple[CallRecord, ...]:
        """Retained records filtered by role and/or iteration."""
        wanted = role.value if isinstance(role, BindingRole) else role
        return tuple(
            record
            for record in self._records
            if (wanted is None or record.role == wanted) and (iter is None or record.iter == iter)
        )

    def totals(self) -> UsageTotals:
        """Totals over every call ever recorded, including evicted detail records."""
        return self._totals

    def tokens_by_role(self) -> dict[str, UsageTotals]:
        """Totals per role, in first-seen order."""
        return dict(self._by_role)

    def tokens_by_iteration(self) -> dict[int, UsageTotals]:
        """Totals per iteration, in ascending order."""
        return {key: self._by_iteration[key] for key in sorted(self._by_iteration)}

    def cache_hit_ratio(self, *, role: BindingRole | str | None = None) -> float:
        """Cached prompt tokens / all prompt tokens, overall or for one role."""
        if role is None:
            return self._totals.cache_hit_ratio
        wanted = role.value if isinstance(role, BindingRole) else role
        return self._by_role.get(wanted, UsageTotals()).cache_hit_ratio

    def estimate_drift(self) -> float | None:
        """Mean signed estimate error over the calls that carried an estimate."""
        if self._drift_samples == 0:
            return None
        return self._drift_sum / self._drift_samples

    def quality_history(self) -> tuple[QualityPoint, ...]:
        """Q per iteration, ascending."""
        return tuple(QualityPoint(iter=key, q=self._quality[key]) for key in sorted(self._quality))

    def token_roi(self, *, iter_from: int | None = None, iter_to: int | None = None) -> TokenROI:
        """Q gained per 10k tokens between two iterations.

        Tokens counted are those spent in ``(iter_from, iter_to]``: the spend that
        produced the later measurement. Defaults to the first and last Q sample.

        Raises:
            TelemetryError: fewer than two Q samples in the requested range.
        """
        points = self.quality_history()
        if iter_from is not None:
            points = tuple(point for point in points if point.iter >= iter_from)
        if iter_to is not None:
            points = tuple(point for point in points if point.iter <= iter_to)
        if len(points) < 2:
            raise TelemetryError(
                "token ROI needs at least two Q measurements in the requested range",
                run_id=self._run_id,
                samples=len(points),
                iter_from=iter_from,
                iter_to=iter_to,
            )
        first, last = points[0], points[-1]
        spent = sum(
            totals.tokens
            for iteration, totals in self._by_iteration.items()
            if first.iter < iteration <= last.iter
        )
        return TokenROI(
            q_before=first.q,
            q_after=last.q,
            tokens=spent,
            iterations=last.iter - first.iter,
        )

    def summary(self) -> TelemetrySummary:
        """The whole spend picture, including ROI when two Q samples exist."""
        roi: TokenROI | None = None
        try:
            roi = self.token_roi()
        except TelemetryError:
            roi = None
        return TelemetrySummary(
            run_id=self._run_id,
            totals=self._totals,
            by_role=self.tokens_by_role(),
            by_iteration=self.tokens_by_iteration(),
            cache_hit_ratio=self.cache_hit_ratio(),
            roi=roi,
            estimate_drift=self.estimate_drift(),
        )


def total_for(records: Iterable[CallRecord]) -> UsageTotals:
    """Fold any iterable of records into one total (helpers for tests and exports)."""
    total = UsageTotals()
    for record in records:
        total = total.plus(record)
    return total


def role_view(records: Sequence[CallRecord]) -> dict[str, UsageTotals]:
    """Group records by role."""
    grouped: dict[str, UsageTotals] = {}
    for record in records:
        grouped[record.role] = grouped.get(record.role, UsageTotals()).plus(record)
    return grouped
