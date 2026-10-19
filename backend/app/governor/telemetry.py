"""Telemetry: one record per provider call, plus the queries the loop needs.

Every call - success or failure - becomes a :class:`CallRecord`. The
:class:`TelemetryStore` aggregates them (in memory, and optionally in the
``telemetry_calls`` table) and answers the questions the governor exists for:

* how many tokens did each role / iteration spend?
* what share of prompt tokens came from the cache?
* **token ROI**: quality points gained per 10k tokens.

Costs come from :mod:`app.core.pricing` and are estimates only; the hard budgets
in :mod:`app.governor.budget` are token-based and never depend on them.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.contracts.ledger import TokenUsage
from app.contracts.quality import QualityReport
from app.core.pricing import estimate_cost

__all__ = [
    "CallRecord",
    "TelemetryStore",
    "cache_hit_ratio_for",
    "token_roi",
    "token_roi_from_reports",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One provider call, as billed by the governor."""

    run_id: str
    iter: int
    task_id: str
    role: str
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    latency_ms: int
    est_cost: float
    ts: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    ok: bool = True
    error_code: str | None = None

    @property
    def total_tokens(self) -> int:
        """Every token the provider reported (prompt full-rate + cached + output)."""
        return self.tokens_in + self.tokens_out + self.tokens_cached

    @property
    def prompt_tokens(self) -> int:
        """Prompt tokens, cached or not."""
        return self.tokens_in + self.tokens_cached

    @property
    def cache_hit_ratio(self) -> float:
        """Share of prompt tokens served from cache, 0.0 when there was no prompt."""
        prompt = self.prompt_tokens
        return 0.0 if prompt == 0 else self.tokens_cached / prompt

    @classmethod
    def from_usage(
        cls,
        *,
        run_id: str,
        iter: int,
        task_id: str,
        role: str,
        model: str,
        usage: TokenUsage,
        latency_ms: int,
        est_cost: float | None = None,
        ok: bool = True,
        error_code: str | None = None,
        ts: datetime | None = None,
    ) -> CallRecord:
        """Build a record from provider usage, estimating cost when not supplied."""
        return cls(
            run_id=run_id,
            iter=iter,
            task_id=task_id,
            role=role,
            model=model,
            tokens_in=usage.in_,
            tokens_out=usage.out,
            tokens_cached=usage.cached,
            latency_ms=latency_ms,
            est_cost=estimate_cost(model, usage) if est_cost is None else est_cost,
            ts=ts or datetime.now(tz=UTC),
            ok=ok,
            error_code=error_code,
        )


# --------------------------------------------------------------------------- maths
def cache_hit_ratio_for(records: Sequence[CallRecord]) -> float:
    """Cache-hit ratio over a set of records (cached / all prompt tokens)."""
    prompt = sum(record.prompt_tokens for record in records)
    return 0.0 if prompt == 0 else sum(record.tokens_cached for record in records) / prompt


def token_roi(q_start: float, q_end: float, tokens_used: int) -> float:
    """Quality gained per 10,000 tokens.

    Args:
        q_start: Q at the start of the window.
        q_end: Q at the end of the window.
        tokens_used: tokens spent in the window.

    Returns:
        ``(q_end - q_start) / (tokens_used / 10_000)``, or ``0.0`` when no tokens
        were spent (dividing by zero would be a lie, and a negative ROI is
        meaningful data: quality fell while tokens burned).

    Example:
        >>> token_roi(60, 70, 20_000)
        5.0
    """
    if tokens_used <= 0:
        return 0.0
    return (float(q_end) - float(q_start)) / (tokens_used / 10_000.0)


def token_roi_from_reports(
    reports: Sequence[QualityReport],
    tokens_per_iter: dict[int, int] | Sequence[int] | None = None,
) -> float:
    """ROI across a run: Q at the last report minus Q at the first, per 10k tokens.

    Args:
        reports: quality reports in iteration order; fewer than two is not a
            measurable window and yields ``0.0``.
        tokens_per_iter: tokens spent per iteration (dict keyed by iteration, or a
            sequence parallel to ``reports``). Required: without spend there is no
            ROI to compute.

    Returns:
        Quality points per 10k tokens between the first and last report.
    """
    if len(reports) < 2 or tokens_per_iter is None:
        return 0.0
    first, last = reports[0], reports[-1]
    if isinstance(tokens_per_iter, dict):
        low, high = min(first.iter, last.iter), max(first.iter, last.iter)
        tokens = sum(
            count for iteration, count in tokens_per_iter.items() if low <= iteration <= high
        )
    else:
        tokens = sum(tokens_per_iter)
    return token_roi(first.Q, last.Q, tokens)


# --------------------------------------------------------------------------- store
class TelemetryStore:
    """Records every call and answers aggregate queries.

    Args:
        session_factory: optional SQLAlchemy ``sessionmaker``. When supplied,
            records are written to ``telemetry_calls`` and reloaded on startup so
            a restarted process can still report a run's spend. Persistence is
            best-effort; the in-memory store is always authoritative for the
            current process.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any] | Any | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._records: list[CallRecord] = []
        self._session_factory = session_factory
        if session_factory is not None:
            self._ensure_table()
            self._restore()

    # -- writes ------------------------------------------------------------
    def record(self, record: CallRecord) -> None:
        """Append one call record (thread-safe, persisted when configured).

        The database write happens *outside* the lock: every query in this store
        takes the same lock, and holding it across disk I/O would make a slow
        write stall the loop controller's metrics reads.
        """
        with self._lock:
            self._records.append(record)
        if self._session_factory is not None:
            self._persist(record)

    def record_call(
        self,
        *,
        run_id: str,
        iter: int,
        task_id: str,
        role: str,
        model: str,
        usage: TokenUsage,
        latency_ms: int,
        est_cost: float | None = None,
        ok: bool = True,
        error_code: str | None = None,
    ) -> CallRecord:
        """Build and store a record from provider usage."""
        return self._record(
            CallRecord.from_usage(
                run_id=run_id,
                iter=iter,
                task_id=task_id,
                role=role,
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                est_cost=est_cost,
                ok=ok,
                error_code=error_code,
            )
        )

    def _record(self, record: CallRecord) -> CallRecord:
        self.record(record)
        return record

    # -- reads -------------------------------------------------------------
    def all_records(self) -> list[CallRecord]:
        """A copy of every record (safe to iterate outside the lock)."""
        with self._lock:
            return list(self._records)

    def records_for_run(self, run_id: str) -> list[CallRecord]:
        """Records belonging to ``run_id``."""
        with self._lock:
            return [record for record in self._records if record.run_id == run_id]

    def tokens_by_role(
        self,
        run_id: str | None = None,
        iteration: int | None = None,
    ) -> dict[str, int]:
        """Tokens per role, optionally scoped to a run and/or iteration."""
        with self._lock:
            totals: dict[str, int] = defaultdict(int)
            for record in self._records:
                if run_id is not None and record.run_id != run_id:
                    continue
                if iteration is not None and record.iter != iteration:
                    continue
                totals[record.role] += record.total_tokens
            return dict(totals)

    def tokens_by_iteration(self, run_id: str | None = None) -> dict[int, int]:
        """Tokens per iteration, optionally scoped to a run."""
        with self._lock:
            totals: dict[int, int] = defaultdict(int)
            for record in self._records:
                if run_id is not None and record.run_id != run_id:
                    continue
                totals[record.iter] += record.total_tokens
            return dict(sorted(totals.items()))

    def cache_hit_ratio(self, run_id: str | None = None, role: str | None = None) -> float:
        """Cache-hit ratio over the selected records."""
        with self._lock:
            selected = [
                record
                for record in self._records
                if (run_id is None or record.run_id == run_id)
                and (role is None or record.role == role)
            ]
        return cache_hit_ratio_for(selected)

    def total_tokens(self, run_id: str | None = None) -> int:
        """Total tokens recorded, optionally scoped to a run."""
        with self._lock:
            return sum(
                record.total_tokens
                for record in self._records
                if run_id is None or record.run_id == run_id
            )

    def total_cost(self, run_id: str | None = None) -> float:
        """Estimated USD recorded, optionally scoped to a run."""
        with self._lock:
            return sum(
                record.est_cost
                for record in self._records
                if run_id is None or record.run_id == run_id
            )

    def avg_latency(self, run_id: str | None = None, role: str | None = None) -> float:
        """Mean provider latency in ms over the selected records."""
        with self._lock:
            values = [
                record.latency_ms
                for record in self._records
                if (run_id is None or record.run_id == run_id)
                and (role is None or record.role == role)
            ]
        return 0.0 if not values else sum(values) / len(values)

    def roi(
        self,
        run_id: str,
        reports: Sequence[QualityReport] | None = None,
        q_start: float | None = None,
        q_end: float | None = None,
    ) -> float:
        """Quality points per 10k tokens for ``run_id``.

        Two forms:

        * ``roi(run_id, reports)`` - first to last report, over the run's tokens;
        * ``roi(run_id, q_start=60, q_end=72)`` - explicit Q window.

        Returns ``0.0`` when the run has no tokens or there is no Q window.
        """
        tokens = self.total_tokens(run_id)
        if tokens == 0:
            return 0.0
        if reports is not None:
            if len(reports) < 2:
                return 0.0
            return token_roi(reports[0].Q, reports[-1].Q, tokens)
        if q_start is not None and q_end is not None:
            return token_roi(q_start, q_end, tokens)
        return 0.0

    def roi_for_window(self, q_start: float, q_end: float, tokens_used: int) -> float:
        """ROI for an arbitrary window, without consulting stored records."""
        return token_roi(q_start, q_end, tokens_used)

    def summary(self, run_id: str | None = None) -> dict[str, Any]:
        """JSON-serialisable aggregate, suitable for logs or ``LoopDecision.evidence``."""
        with self._lock:
            scoped = [
                record for record in self._records if run_id is None or record.run_id == run_id
            ]
            return {
                "call_count": len(scoped),
                "total_tokens": sum(record.total_tokens for record in scoped),
                "total_cost": round(sum(record.est_cost for record in scoped), 6),
                "cache_hit_ratio": round(cache_hit_ratio_for(scoped), 4),
                "avg_latency_ms": round(
                    (sum(record.latency_ms for record in scoped) / len(scoped)) if scoped else 0.0,
                    1,
                ),
                "tokens_by_role": {
                    role: sum(record.total_tokens for record in scoped if record.role == role)
                    for role in sorted({record.role for record in scoped})
                },
                "tokens_by_iteration": {
                    str(iteration): sum(
                        record.total_tokens for record in scoped if record.iter == iteration
                    )
                    for iteration in sorted({record.iter for record in scoped})
                },
            }

    # -- maintenance -------------------------------------------------------
    def clear(self, run_id: str | None = None) -> None:
        """Forget records (all of them, or one run). Intended for tests."""
        with self._lock:
            if run_id is None:
                self._records.clear()
            else:
                self._records = [record for record in self._records if record.run_id != run_id]
            if self._session_factory is not None:
                self._delete(run_id)

    # -- persistence -------------------------------------------------------
    def _ensure_table(self) -> None:
        factory = self._session_factory
        assert factory is not None
        try:
            with factory() as session:
                session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS telemetry_calls (
                            run_id TEXT NOT NULL,
                            iteration INTEGER NOT NULL,
                            task_id TEXT NOT NULL,
                            role TEXT NOT NULL,
                            model TEXT NOT NULL,
                            tokens_in INTEGER NOT NULL,
                            tokens_out INTEGER NOT NULL,
                            tokens_cached INTEGER NOT NULL,
                            latency_ms INTEGER NOT NULL,
                            est_cost REAL NOT NULL,
                            ts TEXT NOT NULL,
                            ok INTEGER NOT NULL DEFAULT 1,
                            error_code TEXT
                        )
                        """
                    )
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - storage failure path
            logger.debug("telemetry table creation failed: %s", exc)

    def _persist(self, record: CallRecord) -> None:
        factory = self._session_factory
        assert factory is not None
        try:
            with factory() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO telemetry_calls
                            (run_id, iteration, task_id, role, model, tokens_in,
                             tokens_out, tokens_cached, latency_ms, est_cost, ts, ok, error_code)
                        VALUES
                            (:run_id, :iteration, :task_id, :role, :model, :tokens_in,
                             :tokens_out, :tokens_cached, :latency_ms, :est_cost, :ts, :ok,
                             :error_code)
                        """
                    ),
                    {
                        "run_id": record.run_id,
                        "iteration": record.iter,
                        "task_id": record.task_id,
                        "role": record.role,
                        "model": record.model,
                        "tokens_in": record.tokens_in,
                        "tokens_out": record.tokens_out,
                        "tokens_cached": record.tokens_cached,
                        "latency_ms": record.latency_ms,
                        "est_cost": record.est_cost,
                        "ts": record.ts.isoformat(),
                        "ok": 1 if record.ok else 0,
                        "error_code": record.error_code,
                    },
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - storage failure path
            logger.debug("telemetry persistence failed: %s", exc)

    def _restore(self) -> None:
        factory = self._session_factory
        assert factory is not None
        try:
            with factory() as session:
                rows = session.execute(
                    text(
                        "SELECT run_id, iteration, task_id, role, model, tokens_in, tokens_out, "
                        "tokens_cached, latency_ms, est_cost, ts, ok, error_code "
                        "FROM telemetry_calls ORDER BY ts"
                    )
                ).fetchall()
        except Exception as exc:  # pragma: no cover - first run has no table
            logger.debug("telemetry restore skipped: %s", exc)
            return
        for row in rows:
            (
                run_id,
                iteration,
                task_id,
                role,
                model,
                tokens_in,
                tokens_out,
                tokens_cached,
                latency_ms,
                est_cost,
                ts,
                ok,
                error_code,
            ) = row
            try:
                parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.now(tz=UTC)
            self._records.append(
                CallRecord(
                    run_id=str(run_id),
                    iter=int(iteration),
                    task_id=str(task_id),
                    role=str(role),
                    model=str(model),
                    tokens_in=int(tokens_in),
                    tokens_out=int(tokens_out),
                    tokens_cached=int(tokens_cached),
                    latency_ms=int(latency_ms),
                    est_cost=float(est_cost or 0.0),
                    ts=parsed,
                    ok=bool(ok),
                    error_code=None if error_code is None else str(error_code),
                )
            )

    def _delete(self, run_id: str | None) -> None:
        factory = self._session_factory
        assert factory is not None
        try:
            with factory() as session:
                if run_id is None:
                    session.execute(text("DELETE FROM telemetry_calls"))
                else:
                    session.execute(
                        text("DELETE FROM telemetry_calls WHERE run_id = :run_id"),
                        {"run_id": run_id},
                    )
                session.commit()
        except Exception as exc:  # pragma: no cover - storage failure path
            logger.debug("telemetry delete failed: %s", exc)
