"""Hierarchical token budgets: account-month -> run -> iteration -> call.

Two rules make this module trustworthy:

* **Check before, record after.** :meth:`BudgetManager.precheck` is called with
  an estimate before a provider call and refuses (``BudgetExceeded``) if any
  ceiling would be crossed; :meth:`BudgetManager.record` is called with the real
  usage afterwards. An under-estimate can therefore never silently overspend: the
  next precheck sees the truth.
* **One lock, every counter.** All reads and writes happen under a single
  re-entrant lock, so concurrent agents cannot interleave a check and a record
  into an overspend. When a ``session_factory`` is supplied the write-through to
  SQLite/Postgres also happens inside that critical section.

Levels, outermost first::

    account-month  # per provider account, from Account.monthly_budget (Step 1)
      run          # policies.run_token_cap, whole run
        iteration  # policies.iteration_token_cap, one loop iteration
          call     # policies.call_input_cap, one prompt (also task.token_budget)

Limits are *inclusive ceilings*: usage at exactly 100 % of a limit is refused
before the next call. A soft warning is logged (and returned) from 80 %.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text

from app.contracts.account import MonthlyBudget
from app.contracts.ledger import TokenUsage
from app.core.errors import BudgetExceeded as CoreBudgetExceeded
from app.core.policies import Policies
from app.core.pricing import estimate_cost

__all__ = [
    "WARNING_THRESHOLD",
    "BudgetExceeded",
    "BudgetLevel",
    "BudgetManager",
    "BudgetSnapshot",
    "BudgetStatus",
    "BudgetWarning",
    "HierarchicalBudget",
    "month_key",
]

logger = logging.getLogger(__name__)

#: Fraction of a ceiling at which a soft warning is raised.
WARNING_THRESHOLD = 0.80
#: How many soft warnings are retained for inspection (the rest are only logged).
WARNING_HISTORY = 200


class BudgetLevel(StrEnum):
    """The four nested levels, outermost first."""

    CALL = "call"
    ITERATION = "iteration"
    RUN = "run"
    ACCOUNT_MONTH = "account_month"


class BudgetExceeded(CoreBudgetExceeded):
    """A call would cross a ceiling.

    Attributes:
        level: the :class:`BudgetLevel` that fired.
        limit: the ceiling at that level.
        used: usage already recorded at that level.
        remaining: headroom before the call (``limit - used``; never negative
            after the fact, but may be small).
    """

    def __init__(
        self,
        message: str,
        *,
        level: str,
        limit: int | float,
        used: int | float,
        remaining: int | float,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            level=str(level),
            limit=limit,
            used=used,
            remaining=remaining,
            **context,
        )
        self.level: str = str(level)
        self.limit: int | float = limit
        self.used: int | float = used
        self.remaining: int | float = remaining


@dataclass(frozen=True)
class BudgetStatus:
    """One level's state at a point in time."""

    level: str
    limit: int | float | None
    used: int | float
    percent: float
    remaining: int | float | None
    is_warning: bool
    is_exceeded: bool


@dataclass(frozen=True)
class BudgetWarning:
    """A soft threshold crossed at one level."""

    level: str
    limit: int | float
    used: int | float
    percent: float
    remaining: int | float
    message: str


@dataclass(frozen=True)
class BudgetSnapshot:
    """Every level at once, for logs and the loop controller's evidence."""

    run: BudgetStatus
    iteration: BudgetStatus | None = None
    call: BudgetStatus | None = None
    account_month: BudgetStatus | None = None


def month_key(account_id: str, today: date | None = None) -> str:
    """Bucket key for a monthly budget: ``<account>:<YYYY-MM>`` (UTC)."""
    day = today or datetime.now(tz=UTC).date()
    return f"{account_id}:{day.year:04d}-{day.month:02d}"


def _status(
    level: str,
    limit: int | float | None,
    used: int | float,
) -> BudgetStatus:
    if limit is None or limit <= 0:
        return BudgetStatus(
            level=level,
            limit=None,
            used=used,
            percent=0.0,
            remaining=None,
            is_warning=False,
            is_exceeded=False,
        )
    percent = float(used) / float(limit)
    return BudgetStatus(
        level=level,
        limit=limit,
        used=used,
        percent=percent,
        remaining=limit - used,
        is_warning=WARNING_THRESHOLD <= percent < 1.0,
        is_exceeded=percent >= 1.0,
    )


class BudgetManager:
    """Nested budget accounting with atomic precheck/record.

    Args:
        policies: effective :class:`Policies` for the run. Supplies the run,
            iteration and call ceilings.
        account_budgets: ``account_id -> MonthlyBudget`` from the account store
            (Step 1). Accounts without an entry have no monthly ceiling.
        run_id: run identifier; also the persistence key. ``None`` means
            in-memory only (no restore across processes).
        session_factory: SQLAlchemy ``sessionmaker`` used to persist counters in
            ``governor_budget``. The daily account roll-up
            (``account_usage_daily``) belongs to the pool's usage sink and is
            deliberately *not* written here: two writers would double-bill.
        clock: ``() -> date`` used for month bucketing (injectable in tests).
    """

    def __init__(
        self,
        policies: Policies,
        *,
        account_budgets: dict[str, MonthlyBudget] | None = None,
        run_id: str | None = None,
        session_factory: Callable[[], Any] | Any | None = None,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._policies = policies
        self._account_budgets: dict[str, MonthlyBudget] = dict(account_budgets or {})
        self._run_id = run_id
        self._session_factory = session_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._persistence_failures = 0

        self._run_tokens = 0
        self._run_cost = 0.0
        self._iteration_tokens: dict[int, int] = {}
        self._account_month_tokens: dict[str, int] = {}
        self._account_month_cost: dict[str, float] = {}
        self._warnings: deque[BudgetWarning] = deque(maxlen=WARNING_HISTORY)

        if session_factory is not None:
            try:
                self._ensure_table()
                self._restore()
            except Exception as exc:  # pragma: no cover - storage failure path
                # Budget enforcement must not depend on the database being up:
                # fall back to in-memory accounting and say so once.
                logger.warning("budget persistence unavailable, continuing in memory: %s", exc)
                self._session_factory = None

    # -- read-only views ---------------------------------------------------
    @property
    def run_used(self) -> int:
        """Tokens recorded against the run so far."""
        with self._lock:
            return self._run_tokens

    @property
    def run_cost(self) -> float:
        """Estimated USD recorded against the run so far."""
        with self._lock:
            return self._run_cost

    @property
    def persistence_failures(self) -> int:
        """Count of durable writes that failed (0 = every record is persisted)."""
        with self._lock:
            return self._persistence_failures

    @property
    def warnings(self) -> list[BudgetWarning]:
        """Soft warnings raised so far (most recent last)."""
        with self._lock:
            return list(self._warnings)

    def iteration_used(self, iteration: int) -> int:
        """Tokens recorded against ``iteration``."""
        with self._lock:
            return self._iteration_tokens.get(iteration, 0)

    def account_month_used(self, account_id: str) -> int:
        """Tokens recorded against ``account_id`` in the current UTC month."""
        with self._lock:
            return self._account_month_tokens.get(self._key(account_id), 0)

    def status(
        self,
        level: BudgetLevel | str,
        *,
        account_id: str | None = None,
        iteration: int | None = None,
        add: int = 0,
    ) -> BudgetStatus:
        """Current status of one level, optionally including ``add`` more tokens."""
        resolved = BudgetLevel(level)
        with self._lock:
            if resolved is BudgetLevel.RUN:
                return _status(resolved.value, self._policies.run_token_cap, self._run_tokens + add)
            if resolved is BudgetLevel.ITERATION:
                if iteration is None:
                    raise ValueError("iteration is required to inspect the iteration budget")
                iteration_limit: int | None = self._policies.iteration_token_cap
                iteration_used = self._iteration_tokens.get(iteration, 0)
                return _status(resolved.value, iteration_limit, iteration_used + add)
            if resolved is BudgetLevel.ACCOUNT_MONTH:
                if account_id is None:
                    raise ValueError("account_id is required to inspect the account-month budget")
                budget = self._account_budgets.get(account_id)
                account_limit: int | None = budget.tokens if budget is not None else None
                account_used = self._account_month_tokens.get(self._key(account_id), 0)
                return _status(resolved.value, account_limit, account_used + add)
            # CALL has no cumulative usage: the ceiling is per prompt.
            return _status(
                resolved.value,
                self._policies.call_input_cap,
                add,
            )

    def snapshot(
        self, *, account_id: str | None = None, iteration: int | None = None
    ) -> BudgetSnapshot:
        """Every relevant level at once (for ``LoopDecision.evidence``)."""
        return BudgetSnapshot(
            run=self.status(BudgetLevel.RUN),
            iteration=(
                self.status(BudgetLevel.ITERATION, iteration=iteration)
                if iteration is not None
                else None
            ),
            call=self.status(BudgetLevel.CALL),
            account_month=(
                self.status(BudgetLevel.ACCOUNT_MONTH, account_id=account_id)
                if account_id is not None
                else None
            ),
        )

    def is_warning(
        self,
        level: BudgetLevel | str,
        *,
        account_id: str | None = None,
        iteration: int | None = None,
    ) -> bool:
        """True when ``level`` sits between 80 % and 100 % of its ceiling."""
        return self.status(level, account_id=account_id, iteration=iteration).is_warning

    def remaining(
        self,
        level: BudgetLevel | str,
        *,
        account_id: str | None = None,
        iteration: int | None = None,
    ) -> int | float | None:
        """Headroom at ``level`` (``None`` when it has no ceiling)."""
        return self.status(level, account_id=account_id, iteration=iteration).remaining

    # -- precheck ----------------------------------------------------------
    def precheck(
        self,
        estimate_tokens: int,
        *,
        account_id: str | None = None,
        iteration: int | None = None,
        task_budget: int | None = None,
        est_cost: float | None = None,
    ) -> list[BudgetWarning]:
        """Refuse a call that would cross a ceiling; return soft warnings.

        Args:
            estimate_tokens: prompt + expected completion tokens, already carrying
                the estimator's safety margin.
            account_id: account whose monthly budget applies.
            iteration: current loop iteration.
            task_budget: the task's own ``token_budget``; treated as an extra
                call-level ceiling, whichever is stricter.
            est_cost: estimated USD for accounts with a monthly ``usd`` budget.

        Returns:
            Warnings for levels between 80 % and 100 % after this call.

        Raises:
            BudgetExceeded: a ceiling would be crossed; ``level`` says which.
        """
        if estimate_tokens < 0:
            raise ValueError("estimate_tokens must be >= 0")
        if task_budget is not None and task_budget <= 0:
            raise ValueError("task_budget must be > 0")

        with self._lock:
            warnings: list[BudgetWarning] = []

            call_limit: int | float = self._policies.call_input_cap
            if task_budget is not None:
                call_limit = min(call_limit, task_budget)

            self._require(
                BudgetLevel.CALL,
                limit=call_limit,
                used=0,
                requested=estimate_tokens,
            )
            self._collect_warning(warnings, BudgetLevel.CALL, call_limit, estimate_tokens)

            if iteration is not None:
                iter_limit = self._policies.iteration_token_cap
                iter_used = self._iteration_tokens.get(iteration, 0)
                self._require(
                    BudgetLevel.ITERATION,
                    limit=iter_limit,
                    used=iter_used,
                    requested=estimate_tokens,
                )
                self._collect_warning(
                    warnings, BudgetLevel.ITERATION, iter_limit, iter_used + estimate_tokens
                )

            run_limit = self._policies.run_token_cap
            self._require(
                BudgetLevel.RUN,
                limit=run_limit,
                used=self._run_tokens,
                requested=estimate_tokens,
            )
            self._collect_warning(
                warnings, BudgetLevel.RUN, run_limit, self._run_tokens + estimate_tokens
            )

            if account_id is not None:
                budget = self._account_budgets.get(account_id)
                if budget is not None and budget.tokens is not None:
                    key = self._key(account_id)
                    used = self._account_month_tokens.get(key, 0)
                    self._require(
                        BudgetLevel.ACCOUNT_MONTH,
                        limit=budget.tokens,
                        used=used,
                        requested=estimate_tokens,
                    )
                    self._collect_warning(
                        warnings, BudgetLevel.ACCOUNT_MONTH, budget.tokens, used + estimate_tokens
                    )
                if budget is not None and budget.usd is not None and est_cost is not None:
                    key = self._key(account_id)
                    spent = self._account_month_cost.get(key, 0.0)
                    if spent + est_cost > budget.usd:
                        raise BudgetExceeded(
                            f"account {account_id} would exceed its monthly USD budget: "
                            f"{spent:.4f} + {est_cost:.4f} > {budget.usd:.4f}",
                            level=BudgetLevel.ACCOUNT_MONTH,
                            limit=budget.usd,
                            used=spent,
                            remaining=max(0.0, budget.usd - spent),
                        )

            for warning in warnings:
                self._warnings.append(warning)
                logger.warning("budget soft warning [%s]: %s", warning.level, warning.message)
            return warnings

    # -- record ------------------------------------------------------------
    def record(
        self,
        usage: TokenUsage | int,
        *,
        account_id: str | None = None,
        iteration: int | None = None,
        role: str | None = None,
        model: str | None = None,
        est_cost: float | None = None,
    ) -> None:
        """Add real usage to every level. Atomic and (when configured) persisted.

        Args:
            usage: the provider's :class:`TokenUsage`; ``total`` counts against
                every cap so cached and full-rate prompt tokens are both billed.
            account_id: account that served the call.
            iteration: loop iteration that made the call.
            role: binding role, accepted for symmetry with the pool's usage
                record; the daily roll-up itself is written by ``SqlUsageSink``.
            model: model id, used only to estimate cost when ``est_cost`` is
                absent.
            est_cost: explicit cost; otherwise estimated from ``model``/``usage``.
        """
        if isinstance(usage, TokenUsage):
            tokens = usage.total
            cost = est_cost if est_cost is not None else estimate_cost(model or "", usage)
        else:
            tokens = int(usage)
            cost = est_cost or 0.0
        if tokens < 0:
            raise ValueError("usage must be >= 0")

        with self._lock:
            self._run_tokens += tokens
            self._run_cost += cost
            if iteration is not None:
                previous = self._iteration_tokens.get(iteration, 0)
                self._iteration_tokens[iteration] = previous + tokens
            if account_id is not None:
                key = self._key(account_id)
                self._account_month_tokens[key] = self._account_month_tokens.get(key, 0) + tokens
                self._account_month_cost[key] = self._account_month_cost.get(key, 0.0) + cost

            if self._session_factory is not None:
                self._persist(
                    tokens=tokens,
                    cost=cost,
                    account_id=account_id,
                    iteration=iteration,
                )

    # -- lifecycle ---------------------------------------------------------
    def add_account_budget(self, account_id: str, budget: MonthlyBudget) -> None:
        """Register or replace an account's monthly budget (hot-swap safe)."""
        with self._lock:
            self._account_budgets[account_id] = budget

    # -- internals ---------------------------------------------------------
    def _key(self, account_id: str) -> str:
        return month_key(account_id, self._today())

    def _today(self) -> date:
        if self._clock is not None:
            value = self._clock()
            return value
        return datetime.now(tz=UTC).date()

    def _require(
        self,
        level: BudgetLevel,
        *,
        limit: int | float,
        used: int | float,
        requested: int | float,
    ) -> None:
        if used + requested <= limit:
            return
        raise BudgetExceeded(
            f"{level.value} budget exceeded: {used} used + {requested} requested > {limit}",
            level=level,
            limit=limit,
            used=used,
            remaining=max(0, limit - used),
        )

    def _collect_warning(
        self,
        sink: list[BudgetWarning],
        level: BudgetLevel,
        limit: int | float,
        used: int | float,
    ) -> None:
        if limit <= 0:
            return
        percent = float(used) / float(limit)
        if WARNING_THRESHOLD <= percent < 1.0:
            sink.append(
                BudgetWarning(
                    level=level.value,
                    limit=limit,
                    used=used,
                    percent=percent,
                    remaining=limit - used,
                    message=f"{level.value} at {percent:.0%} of {limit}",
                )
            )

    # -- persistence -------------------------------------------------------
    def _ensure_table(self) -> None:
        factory = self._session_factory
        assert factory is not None
        with factory() as session:
            session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS governor_budget (
                        run_id TEXT NOT NULL,
                        iteration INTEGER NOT NULL,
                        account_id TEXT NOT NULL,
                        month TEXT NOT NULL,
                        tokens_used INTEGER NOT NULL DEFAULT 0,
                        cost_used REAL NOT NULL DEFAULT 0.0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, iteration, account_id, month)
                    )
                    """
                )
            )
            session.commit()

    def _persist(
        self,
        *,
        tokens: int,
        cost: float,
        account_id: str | None,
        iteration: int | None,
    ) -> None:
        """Write the counters through to ``governor_budget``.

        ``account_usage_daily`` is owned by the provider pool's usage sink
        (:class:`app.accounts.service.SqlUsageSink`), which already receives one
        record per call. Writing it here as well would count every token twice
        and inflate the per-account monthly spend that the Step-1 budgets read.
        """
        factory = self._session_factory
        assert factory is not None
        run_id = self._run_id or "unbound-run"
        iteration_key = iteration if iteration is not None else -1
        account_key = account_id or "__run__"
        bucket = self._key(account_id) if account_id is not None else "__run__"
        try:
            with factory() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO governor_budget
                            (run_id, iteration, account_id, month,
                             tokens_used, cost_used, updated_at)
                        VALUES
                            (:run_id, :iteration, :account_id, :month,
                             :tokens, :cost, :updated_at)
                        ON CONFLICT(run_id, iteration, account_id, month)
                        DO UPDATE SET
                            tokens_used = tokens_used + :tokens,
                            cost_used = cost_used + :cost,
                            updated_at = :updated_at
                        """
                    ),
                    {
                        "run_id": run_id,
                        "iteration": iteration_key,
                        "account_id": account_key,
                        "month": bucket,
                        "tokens": tokens,
                        "cost": cost,
                        "updated_at": datetime.now(tz=UTC).isoformat(),
                    },
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - storage failure path
            # The in-memory counters stay correct, but a caller that believes
            # ``record`` is durable must be able to see that it is not.
            self._persistence_failures += 1
            if self._persistence_failures == 1:
                logger.warning(
                    "budget persistence failed for run %s; counters continue in memory "
                    "and will not be restored after a restart: %s",
                    run_id,
                    exc,
                )
            else:
                logger.debug("budget persistence failed again: %s", exc)

    def _restore(self) -> None:
        """Reload counters for ``run_id`` from ``governor_budget``."""
        factory = self._session_factory
        assert factory is not None
        if not self._run_id:
            return
        try:
            with factory() as session:
                rows = session.execute(
                    text(
                        "SELECT iteration, account_id, month, tokens_used, cost_used "
                        "FROM governor_budget WHERE run_id = :run_id"
                    ),
                    {"run_id": self._run_id},
                ).fetchall()
        except Exception as exc:  # pragma: no cover - first run has no table
            logger.debug("budget restore skipped: %s", exc)
            return
        for iteration, account_id, bucket, tokens, cost in rows:
            tokens = int(tokens or 0)
            cost = float(cost or 0.0)
            self._run_tokens += tokens
            self._run_cost += cost
            if iteration is not None and int(iteration) >= 0:
                key = int(iteration)
                self._iteration_tokens[key] = self._iteration_tokens.get(key, 0) + tokens
            if account_id and account_id != "__run__":
                self._account_month_tokens[str(bucket)] = (
                    self._account_month_tokens.get(str(bucket), 0) + tokens
                )
                self._account_month_cost[str(bucket)] = (
                    self._account_month_cost.get(str(bucket), 0.0) + cost
                )


#: Historic alias kept for call sites that predate the rename.
HierarchicalBudget = BudgetManager
