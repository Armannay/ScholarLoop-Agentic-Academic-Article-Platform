"""Nested token budgets: account-month -> run -> iteration -> call.

Every provider call is admitted by exactly one :class:`BudgetGovernor`, which enforces
four nested ceilings before the call and settles the real usage afterwards:

===============  =========================================  ==========================
level            limit                                      source
===============  =========================================  ==========================
``account_month``  tokens/usd per account per calendar month   ``Account.monthly_budget``
``run``            ``Policies.run_token_cap``                 ``config/policies.yaml``
``iteration``      ``Policies.iteration_token_cap``           ``config/policies.yaml``
``call``           ``Policies.call_input_cap`` + task budget  policies + ``Task``
===============  =========================================  ==========================

Design rules (each one is load-bearing):

* **Refuse, never truncate.** :meth:`BudgetGovernor.precheck` raises
  :class:`~app.core.errors.BudgetExceeded` carrying ``level`` and ``remaining``; it
  never shaves a prompt to make it fit. Trim decisions belong to the context builder,
  which documents its trim order.
* **Reserve, then settle.** ``precheck`` *reserves* the estimate and ``record`` settles
  it against the provider's real usage. A reservation is part of the committed total,
  so two concurrent calls cannot both spend the last 1,000 tokens. A call that never
  reached the provider is refunded with :meth:`BudgetGovernor.release`.
* **Soft warning at 80 %, hard stop at 100 %.** Crossing ``soft_warn_at`` of any level
  logs once and calls the optional ``on_soft_warning`` hook; reaching the limit refuses
  the next call.
* **Atomic and persisted.** Counters live behind :class:`BudgetStore`. The shipped
  :class:`InMemoryBudgetStore` is enough for one process and for tests;
  :class:`FileBudgetStore` survives a restart by writing the counter document with an
  atomic ``Path.replace``. A SQL store is a drop-in for multi-worker deployments - see
  the HANDOFF's CONTRACT-CHANGE-REQUEST for a ``budget_counters`` table.
* **Single process per file.** ``FileBudgetStore`` serialises threads, not processes:
  two workers sharing one file would lose updates (the JSON file itself never
  corrupts, because every write is a whole-file atomic replace).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from app.contracts.account import MonthlyBudget
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.contracts.task import Task
from app.core.clock import utc_now
from app.core.errors import BudgetExceeded, ScholarLoopError
from app.core.logging import get_logger
from app.core.policies import DEFAULT_POLICIES, Policies
from app.providers.base import estimate_tokens, prompt_tokens

__all__ = [
    "DEFAULT_SOFT_WARN",
    "LEVEL_ORDER",
    "BudgetEstimate",
    "BudgetGovernor",
    "BudgetLevel",
    "BudgetLimits",
    "BudgetScope",
    "BudgetStatus",
    "BudgetStore",
    "BudgetStoreError",
    "FileBudgetStore",
    "InMemoryBudgetStore",
    "LevelUsage",
    "Reservation",
]

logger = get_logger(__name__)

#: Fraction of a limit at which a level is reported as a soft warning.
DEFAULT_SOFT_WARN: Final[float] = 0.8


class BudgetLevel(StrEnum):
    """The four nested scopes, outermost first."""

    ACCOUNT_MONTH = "account_month"
    RUN = "run"
    ITERATION = "iteration"
    CALL = "call"


#: Check order: the outer scope is named first, because it is the one that binds.
LEVEL_ORDER: Final[tuple[BudgetLevel, ...]] = (
    BudgetLevel.ACCOUNT_MONTH,
    BudgetLevel.RUN,
    BudgetLevel.ITERATION,
    BudgetLevel.CALL,
)


class BudgetStoreError(ScholarLoopError):
    """The persisted counter document is unreadable or malformed."""

    code = "budget_store_error"
    http_status = 500


# --------------------------------------------------------------------------- estimates
@dataclass(frozen=True)
class BudgetEstimate:
    """What one call is projected to spend, in the units the caps measure.

    ``tokens`` is prompt + completion: the same non-overlapping convention as
    :class:`~app.contracts.ledger.TokenUsage` (cached prompt tokens are still prompt
    tokens and still count against a token cap).
    """

    input_tokens: int
    output_tokens: int = 0
    cost_usd: float = 0.0
    source: str = "caller"

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token estimates must be >= 0")
        if self.cost_usd < 0.0:
            raise ValueError("cost estimates must be >= 0.0")

    @property
    def tokens(self) -> int:
        """Total tokens this call is projected to consume."""
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_request(
        cls,
        request: LLMRequest,
        *,
        cost_usd: float = 0.0,
        extra_input_tokens: int = 0,
    ) -> BudgetEstimate:
        """Estimate a request with the shared ``chars / 4`` convention.

        The JSON schema is counted as input: it is sent on every structured call and
        pretending otherwise would under-count exactly the calls that cost most.
        """
        schema_tokens = 0
        if request.json_schema is not None:
            schema_text = json.dumps(request.json_schema, sort_keys=True, separators=(",", ":"))
            schema_tokens = estimate_tokens(schema_text)
        return cls(
            input_tokens=prompt_tokens(request) + schema_tokens + extra_input_tokens,
            output_tokens=request.max_output_tokens,
            cost_usd=cost_usd,
            source="heuristic",
        )

    @classmethod
    def from_usage(cls, usage: TokenUsage, *, cost_usd: float = 0.0) -> BudgetEstimate:
        """Turn measured usage into the estimate shape, for settling by actuals."""
        return cls(
            input_tokens=usage.in_ + usage.cached,
            output_tokens=usage.out,
            cost_usd=cost_usd,
            source="provider",
        )


# --------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class BudgetLimits:
    """The four ceilings, resolved from policies plus the account's own budget."""

    run_tokens: int
    iteration_tokens: int
    call_input_tokens: int
    account_month_tokens: int | None = None
    account_month_usd: float | None = None
    soft_warn_at: float = DEFAULT_SOFT_WARN

    def __post_init__(self) -> None:
        for name in ("run_tokens", "iteration_tokens", "call_input_tokens"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.iteration_tokens > self.run_tokens:
            raise ValueError(
                "iteration_tokens must not exceed run_tokens "
                f"({self.iteration_tokens} > {self.run_tokens})"
            )
        if self.call_input_tokens >= self.iteration_tokens:
            raise ValueError(
                "call_input_tokens must stay below iteration_tokens "
                f"({self.call_input_tokens} >= {self.iteration_tokens})"
            )
        if not 0.0 < self.soft_warn_at <= 1.0:
            raise ValueError(f"soft_warn_at must be in (0.0, 1.0], got {self.soft_warn_at}")
        for value in (self.account_month_tokens, self.account_month_usd):
            if value is not None and value < 0:
                raise ValueError("account budgets must be >= 0")

    @classmethod
    def from_policies(
        cls,
        policies: Policies = DEFAULT_POLICIES,
        *,
        monthly_budget: MonthlyBudget | None = None,
        soft_warn_at: float = DEFAULT_SOFT_WARN,
    ) -> BudgetLimits:
        """Resolve the nested ceilings from policies and an optional account budget.

        An absent ``MonthlyBudget`` (or a ``None`` field inside it) means "no account
        ceiling": the run and iteration caps still apply, so a call can never become
        free just because the user set no monthly limit.
        """
        budget = monthly_budget if monthly_budget is not None else MonthlyBudget()
        return cls(
            run_tokens=policies.run_token_cap,
            iteration_tokens=policies.iteration_token_cap,
            call_input_tokens=policies.call_input_cap,
            account_month_tokens=budget.tokens,
            account_month_usd=budget.usd,
            soft_warn_at=soft_warn_at,
        )

    def limit_for(self, level: BudgetLevel) -> int | None:
        """Token limit for one level, ``None`` when the level is unbounded."""
        return {
            BudgetLevel.RUN: self.run_tokens,
            BudgetLevel.ITERATION: self.iteration_tokens,
            BudgetLevel.CALL: self.call_input_tokens,
            BudgetLevel.ACCOUNT_MONTH: self.account_month_tokens,
        }.get(level)

    def usd_limit_for(self, level: BudgetLevel) -> float | None:
        """USD limit for one level, ``None`` when unset."""
        return self.account_month_usd if level is BudgetLevel.ACCOUNT_MONTH else None


# --------------------------------------------------------------------------- scope
@dataclass(frozen=True)
class BudgetScope:
    """Which counters a governor reads and writes.

    ``period`` is the calendar month for the account-level counter (``YYYY-MM``); it
    defaults to the current UTC month so a run that crosses midnight on the 1st starts
    a fresh account counter instead of reusing last month's.
    """

    run_id: str
    iteration: int = 0
    account_id: str | None = None
    period: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.iteration < 0:
            raise ValueError("iteration must be >= 0")
        if self.account_id is not None and not self.account_id:
            raise ValueError("account_id must be None or non-empty")
        if self.account_id is not None and not self.period:
            object.__setattr__(self, "period", utc_now().strftime("%Y-%m"))
        if self.account_id is None and self.period:
            raise ValueError("period is only meaningful when account_id is set")

    @property
    def run_key(self) -> str:
        """Counter key for the whole run."""
        return f"run:{self.run_id}"

    @property
    def iteration_key(self) -> str:
        """Counter key for one iteration of the run."""
        return f"run:{self.run_id}:iter:{self.iteration}"

    @property
    def account_key(self) -> str | None:
        """Counter key for the account's calendar month, or ``None`` when unbound."""
        if self.account_id is None:
            return None
        return f"account:{self.account_id}:{self.period}"

    def with_iteration(self, iteration: int) -> BudgetScope:
        """This scope moved to another iteration (budgets are monotonic in a run)."""
        if iteration < self.iteration:
            raise ValueError(
                f"iteration moves forward only ({iteration} < {self.iteration}); "
                "a new run gets a new governor"
            )
        return BudgetScope(
            run_id=self.run_id,
            iteration=iteration,
            account_id=self.account_id,
            period=self.period,
        )


# --------------------------------------------------------------------------- usage views
@dataclass(frozen=True)
class LevelUsage:
    """Committed usage against one level's limit."""

    level: BudgetLevel
    used: int = 0
    limit: int | None = None
    in_flight: int = 0
    soft_warn_at: float = DEFAULT_SOFT_WARN
    cost_used_usd: float = 0.0
    cost_limit_usd: float | None = None

    @property
    def remaining(self) -> int | None:
        """Tokens left before the hard stop, or ``None`` when unbounded."""
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    @property
    def utilization(self) -> float:
        """Used / limit, ``0.0`` when the level is unbounded."""
        if self.limit is None or self.limit == 0:
            return 0.0
        return self.used / self.limit

    @property
    def cost_utilization(self) -> float:
        """Spend / USD limit, ``0.0`` when no USD limit is set."""
        if self.cost_limit_usd is None or self.cost_limit_usd <= 0.0:
            return 0.0
        return self.cost_used_usd / self.cost_limit_usd

    @property
    def over_limit(self) -> bool:
        """True once the committed total has reached the hard stop."""
        if self.limit is None:
            return False
        if self.limit > 0 and self.used >= self.limit:
            return True
        return self.cost_limit_usd is not None and self.cost_used_usd >= self.cost_limit_usd

    @property
    def is_soft_warning(self) -> bool:
        """True from ``soft_warn_at`` of the limit (or of the USD limit) onwards."""
        tokens_warn = self.limit is not None and self.utilization >= self.soft_warn_at
        usd_warn = self.cost_limit_usd is not None and self.cost_utilization >= self.soft_warn_at
        return tokens_warn or usd_warn

    def projected(self, estimate: BudgetEstimate) -> LevelUsage:
        """This level's usage if ``estimate`` were admitted now."""
        return LevelUsage(
            level=self.level,
            used=self.used + estimate.tokens,
            limit=self.limit,
            in_flight=self.in_flight,
            soft_warn_at=self.soft_warn_at,
            cost_used_usd=self.cost_used_usd + estimate.cost_usd,
            cost_limit_usd=self.cost_limit_usd,
        )


@dataclass(frozen=True)
class BudgetStatus:
    """A consistent snapshot of every level, as returned by precheck/record."""

    scope: BudgetScope
    levels: tuple[LevelUsage, ...]

    def level(self, level: BudgetLevel) -> LevelUsage:
        """Usage for one level."""
        for usage in self.levels:
            if usage.level is level:
                return usage
        raise KeyError(f"no usage recorded for level {level.value!r}")

    @property
    def run(self) -> LevelUsage:
        return self.level(BudgetLevel.RUN)

    @property
    def iteration(self) -> LevelUsage:
        return self.level(BudgetLevel.ITERATION)

    @property
    def account_month(self) -> LevelUsage:
        return self.level(BudgetLevel.ACCOUNT_MONTH)

    @property
    def call(self) -> LevelUsage:
        return self.level(BudgetLevel.CALL)

    @property
    def warnings(self) -> tuple[BudgetLevel, ...]:
        """Levels at or past the soft warning, outer scope first."""
        return tuple(usage.level for usage in self.levels if usage.is_soft_warning)

    @property
    def stopped(self) -> bool:
        """True when at least one level has hit its hard stop."""
        return any(usage.over_limit for usage in self.levels)

    @property
    def total_used(self) -> int:
        """Committed tokens for the run (the run counter, not a sum of levels)."""
        return self.run.used


@dataclass
class Reservation:
    """Tokens held for one in-flight call; settle or release it, never both.

    A reservation is a *claim* on the run and iteration counters. ``settled`` flips on
    :meth:`BudgetGovernor.record` or :meth:`BudgetGovernor.release`, and settling twice
    is a no-op so a retry path cannot double-refund.
    """

    scope: BudgetScope
    estimate: BudgetEstimate
    keys: tuple[str, ...]
    reserved_tokens: int
    reserved_cost_usd: float = 0.0
    settled: bool = False

    @property
    def is_open(self) -> bool:
        """True while the call has neither been recorded nor refunded."""
        return not self.settled


# --------------------------------------------------------------------------- stores
@runtime_checkable
class BudgetStore(Protocol):
    """Durable counter storage with mutual exclusion.

    ``lock`` must exclude other writers of the same counters for the whole
    read-modify-write cycle that :class:`BudgetGovernor` performs; ``commit`` must be
    all-or-nothing so a crash cannot leave a half-applied reservation.
    """

    def lock(self) -> AbstractContextManager[None]:
        """Hold the write lock for the duration of the block."""
        ...

    def snapshot(self) -> dict[str, float]:
        """Read every counter, keyed by the governor's counter keys."""
        ...

    def commit(self, counters: Mapping[str, float]) -> None:
        """Replace the counter document with ``counters``."""
        ...


class InMemoryBudgetStore:
    """Process-local store: the default for tests and single-process runs."""

    def __init__(self, counters: Mapping[str, float] | None = None) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, float] = {
            str(key): float(value) for key, value in (counters or {}).items()
        }

    @contextmanager
    def lock(self) -> Iterator[None]:
        """RLock: re-entrant, so a callback may read status inside a transaction."""
        with self._lock:
            yield

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._counters)

    def commit(self, counters: Mapping[str, float]) -> None:
        with self._lock:
            self._counters = {str(key): float(value) for key, value in counters.items()}


class FileBudgetStore:
    """One JSON counter document on disk, written with an atomic replace.

    Every transaction re-reads the file, so a second governor in the same process sees
    the first one's spend. ``Path.replace`` makes the write atomic: a reader either sees
    the previous document or the next one, never a partial file.

    Not a multi-process lock. Running two uvicorn workers against one file loses
    updates; use a SQL store for that (HANDOFF).
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """Location of the counter document."""
        return self._path

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return _read_counters(self._path)

    def commit(self, counters: Mapping[str, float]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {str(key): float(value) for key, value in counters.items()}
            temporary = self._path.with_name(f".{self._path.name}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                temporary.replace(self._path)
            finally:
                temporary.unlink(missing_ok=True)


def _would_exceed(usage: LevelUsage) -> bool:
    """True when admitting ``usage``'s projection would pass the hard stop.

    Reaching the limit exactly is allowed (the last token is still spendable); the
    *next* call is refused because its projection would exceed a committed total that
    already equals the limit.
    """
    if usage.limit is not None and usage.used > usage.limit:
        return True
    return usage.cost_limit_usd is not None and usage.cost_used_usd > usage.cost_limit_usd


def _read_counters(path: Path) -> dict[str, float]:
    """Read the counter document, or an empty mapping when it does not exist yet."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetStoreError(f"unreadable budget store at {path}: {exc}", path=str(path)) from exc
    if not isinstance(raw, dict):
        raise BudgetStoreError(
            f"budget store at {path} must hold an object, got {type(raw).__name__}",
            path=str(path),
        )
    counters: dict[str, float] = {}
    for key, value in raw.items():
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        if not isinstance(key, str) or not numeric:
            raise BudgetStoreError(
                f"budget store at {path} has a non-numeric counter", path=str(path)
            )
        counters[key] = float(value)
    return counters


# --------------------------------------------------------------------------- governor
class BudgetGovernor:
    """Admits, accounts for and refuses provider calls against nested token budgets.

    Args:
        limits: the four ceilings, usually :meth:`BudgetLimits.from_policies`.
        scope: which run/iteration/account this governor bills.
        store: counter storage; in-memory by default.
        on_soft_warning: called once per level when it crosses ``soft_warn_at``.

    The governor is synchronous on purpose: admission control must be cheap, atomic and
    independent of the event loop, and provider calls are where the async lives.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        scope: BudgetScope,
        store: BudgetStore | None = None,
        on_soft_warning: Callable[[LevelUsage], None] | None = None,
    ) -> None:
        if (
            limits.account_month_tokens is not None or limits.account_month_usd is not None
        ) and scope.account_id is None:
            raise ValueError(
                "an account monthly budget needs a scope account_id to bill; "
                "otherwise the ceiling would silently never apply"
            )
        self._limits = limits
        self._scope = scope
        self._store: BudgetStore = store if store is not None else InMemoryBudgetStore()
        self._on_soft_warning = on_soft_warning
        self._warned: set[tuple[str, BudgetLevel]] = set()

    # -- construction ------------------------------------------------------
    @classmethod
    def for_run(
        cls,
        run_id: str,
        *,
        policies: Policies = DEFAULT_POLICIES,
        account_id: str | None = None,
        monthly_budget: MonthlyBudget | None = None,
        period: str = "",
        store: BudgetStore | None = None,
        soft_warn_at: float = DEFAULT_SOFT_WARN,
        on_soft_warning: Callable[[LevelUsage], None] | None = None,
    ) -> BudgetGovernor:
        """Build a governor from the policy file plus one account's monthly budget."""
        limits = BudgetLimits.from_policies(
            policies, monthly_budget=monthly_budget, soft_warn_at=soft_warn_at
        )
        scope = BudgetScope(run_id=run_id, account_id=account_id, period=period)
        return cls(
            limits,
            scope=scope,
            store=store,
            on_soft_warning=on_soft_warning,
        )

    # -- introspection -----------------------------------------------------
    @property
    def limits(self) -> BudgetLimits:
        """The ceilings this governor enforces."""
        return self._limits

    @property
    def scope(self) -> BudgetScope:
        """The run/iteration/account being billed."""
        return self._scope

    @property
    def store(self) -> BudgetStore:
        """The counter store, exposed so a run can persist or share it."""
        return self._store

    def advance_iteration(self, iteration: int) -> None:
        """Bill subsequent calls to ``iteration`` (never moves backwards)."""
        self._scope = self._scope.with_iteration(iteration)

    def bind_account(self, account_id: str, *, period: str = "") -> None:
        """Bill the account-month level from now on (accounts resolve at call time)."""
        self._scope = BudgetScope(
            run_id=self._scope.run_id,
            iteration=self._scope.iteration,
            account_id=account_id,
            period=period or self._scope.period,
        )

    def status(self) -> BudgetStatus:
        """Snapshot every level without reserving anything."""
        with self._store.lock():
            counters = self._store.snapshot()
        return self._status_from(counters)

    # -- admission ---------------------------------------------------------
    def precheck(
        self,
        estimate: BudgetEstimate | LLMRequest | int,
        *,
        role: str | None = None,
        task: Task | None = None,
    ) -> Reservation:
        """Reserve ``estimate`` or raise :class:`BudgetExceeded`.

        Accepts the estimate shape, a bare token count (treated as input tokens) or an
        :class:`~app.contracts.llm.LLMRequest` so it satisfies the
        :class:`app.agents.base.Governor` seam as well as this module's own contract.

        Raises:
            BudgetExceeded: carrying ``level`` (the binding scope), ``remaining``,
                ``limit`` and ``used``. The outermost failing level wins.
        """
        projected = self._coerce(estimate)
        if task is not None and projected.tokens > task.token_budget:
            raise BudgetExceeded(
                f"call projects {projected.tokens} tokens; task {task.id} budget is "
                f"{task.token_budget}",
                level=BudgetLevel.CALL.value,
                remaining=max(0, task.token_budget - projected.tokens),
                limit=task.token_budget,
                used=projected.tokens,
                run_id=self._scope.run_id,
                iter=self._scope.iteration,
                task_id=task.id,
                **({"role": role} if role is not None else {}),
            )
        with self._store.lock():
            counters = self._store.snapshot()
            levels = self._projected_levels(counters, projected)
            for usage in levels:
                if not _would_exceed(usage):
                    continue
                limit = usage.limit or 0
                committed = self._level(counters, usage.level).used
                raise BudgetExceeded(
                    f"{usage.level.value} budget would be exceeded: "
                    f"{usage.used} committed of {limit} "
                    f"(call projects {projected.tokens} tokens)",
                    level=usage.level.value,
                    remaining=max(0, limit - committed),
                    limit=limit,
                    used=usage.used,
                    run_id=self._scope.run_id,
                    iter=self._scope.iteration,
                    **({"role": role} if role is not None else {}),
                    **({"task_id": task.id} if task is not None else {}),
                )
            keys = self._counter_keys()
            new = dict(counters)
            for key in keys:
                new[key] = new.get(key, 0.0) + projected.tokens
            if projected.cost_usd:
                for key in keys:
                    usd_key = f"{key}:usd"
                    new[usd_key] = new.get(usd_key, 0.0) + projected.cost_usd
            self._store.commit(new)
            status = self._status_from(new)
        self._emit_warnings(status)
        return Reservation(
            scope=self._scope,
            estimate=projected,
            keys=keys,
            reserved_tokens=projected.tokens,
            reserved_cost_usd=projected.cost_usd,
        )

    # -- accounting --------------------------------------------------------
    def record(
        self,
        usage: TokenUsage,
        *,
        reservation: Reservation | None = None,
        cost_usd: float | None = None,
    ) -> BudgetStatus:
        """Settle real usage atomically, refunding the reservation it replaces.

        Without a reservation (for example a provider that answered before admission
        control was introduced) the usage is simply added. Recording the same
        reservation twice is a no-op: the second call re-reads the counters and commits
        them unchanged, so a retry cannot inflate a budget.
        """
        if cost_usd is not None and cost_usd < 0.0:
            raise ValueError("cost_usd must be >= 0.0")
        cost = cost_usd if cost_usd is not None else 0.0
        with self._store.lock():
            counters = self._store.snapshot()
            if reservation is not None and not reservation.is_open:
                logger.warning(
                    "reservation for run %s was already settled; duplicate record ignored",
                    reservation.scope.run_id,
                )
                return self._status_from(counters)
            if reservation is not None and reservation.is_open:
                for key in reservation.keys:
                    counters[key] = max(0.0, counters.get(key, 0.0) - reservation.reserved_tokens)
                    if reservation.reserved_cost_usd:
                        usd_key = f"{key}:usd"
                        counters[usd_key] = max(
                            0.0, counters.get(usd_key, 0.0) - reservation.reserved_cost_usd
                        )
                reservation.settled = True
            # Bill the scopes the call was admitted against. The iteration may have
            # advanced while the call was in flight; the spend still belongs to the
            # iteration that made it, not to the current one.
            keys = reservation.keys if reservation is not None else self._counter_keys()
            for key in keys:
                counters[key] = counters.get(key, 0.0) + usage.total
                if cost:
                    usd_key = f"{key}:usd"
                    counters[usd_key] = counters.get(usd_key, 0.0) + cost
            self._store.commit(counters)
            status = self._status_from(counters)
        self._emit_warnings(status)
        return status

    def release(self, reservation: Reservation) -> BudgetStatus:
        """Refund a reservation for a call that never reached the provider."""
        with self._store.lock():
            counters = self._store.snapshot()
            if reservation.is_open:
                for key in reservation.keys:
                    counters[key] = max(0.0, counters.get(key, 0.0) - reservation.reserved_tokens)
                    if reservation.reserved_cost_usd:
                        usd_key = f"{key}:usd"
                        counters[usd_key] = max(
                            0.0, counters.get(usd_key, 0.0) - reservation.reserved_cost_usd
                        )
                reservation.settled = True
            self._store.commit(counters)
            return self._status_from(counters)

    # -- internals ---------------------------------------------------------
    def _coerce(self, estimate: BudgetEstimate | LLMRequest | int) -> BudgetEstimate:
        """Accept the estimate shape, a bare input-token count or a request."""
        if isinstance(estimate, BudgetEstimate):
            return estimate
        if isinstance(estimate, LLMRequest):
            return BudgetEstimate.from_request(estimate)
        if isinstance(estimate, bool) or not isinstance(estimate, int):
            raise TypeError(
                f"estimate must be BudgetEstimate, LLMRequest or int, got {type(estimate).__name__}"
            )
        return BudgetEstimate(input_tokens=estimate)

    def _counter_keys(self) -> tuple[str, ...]:
        """Every counter a reservation touches, outer scope first."""
        keys: list[str] = []
        account_key = self._scope.account_key
        if account_key is not None:
            keys.append(account_key)
        keys.extend((self._scope.run_key, self._scope.iteration_key))
        return tuple(keys)

    def _level(self, counters: Mapping[str, float], level: BudgetLevel) -> LevelUsage:
        """Committed usage for one level, without projection."""
        key = self._key_for(level)
        used = 0 if key is None else int(counters.get(key, 0.0))
        cost = 0.0 if key is None else float(counters.get(f"{key}:usd", 0.0))
        return LevelUsage(
            level=level,
            used=used,
            limit=self._limits.limit_for(level),
            soft_warn_at=self._limits.soft_warn_at,
            cost_used_usd=cost,
            cost_limit_usd=self._limits.usd_limit_for(level),
        )

    def _projected_levels(
        self, counters: Mapping[str, float], estimate: BudgetEstimate
    ) -> tuple[LevelUsage, ...]:
        """Usage per level if ``estimate`` were admitted, outer scope first."""
        levels: list[LevelUsage] = []
        for level in LEVEL_ORDER:
            usage = self._level(counters, level)
            if level is BudgetLevel.CALL:
                limit = self._limits.call_input_tokens
                levels.append(
                    LevelUsage(
                        level=level,
                        used=estimate.input_tokens,
                        limit=limit,
                        soft_warn_at=self._limits.soft_warn_at,
                    )
                )
            else:
                levels.append(usage.projected(estimate))
        return tuple(levels)

    def _status_from(self, counters: Mapping[str, float]) -> BudgetStatus:
        """Snapshot shape built from raw counters."""
        return BudgetStatus(
            scope=self._scope,
            levels=tuple(self._level(counters, level) for level in LEVEL_ORDER),
        )

    def _key_for(self, level: BudgetLevel) -> str | None:
        """Counter key for a level, ``None`` for a level that keeps no counter."""
        if level is BudgetLevel.ACCOUNT_MONTH:
            return self._scope.account_key
        if level is BudgetLevel.RUN:
            return self._scope.run_key
        if level is BudgetLevel.ITERATION:
            return self._scope.iteration_key
        return None

    def _emit_warnings(self, status: BudgetStatus) -> None:
        """Warn once per level that has crossed the soft-warning threshold."""
        for usage in status.levels:
            if not usage.is_soft_warning:
                continue
            key = self._key_for(usage.level)
            token = (key or usage.level.value, usage.level)
            if token in self._warned:
                continue
            self._warned.add(token)
            if usage.over_limit:
                detail = "hard stop reached"
            else:
                detail = f"{usage.utilization:.0%} of the limit used"
            logger.warning(
                "budget %s at %s/%s tokens (%s) for run %s",
                usage.level.value,
                usage.used,
                usage.limit,
                detail,
                self._scope.run_id,
            )
            if self._on_soft_warning is not None:
                self._on_soft_warning(usage)
