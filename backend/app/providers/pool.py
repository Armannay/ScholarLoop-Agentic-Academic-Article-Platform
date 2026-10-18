"""ProviderPool: account resolution at **call** time, with strategies and a breaker.

The pool is the only place the run loop touches providers. For every call it:

1. reads the caller's :class:`~app.contracts.binding.AgentBinding` (roles include
   ``planner``);
2. picks a candidate account by strategy (``dedicated``, ``round_robin``,
   ``failover``), skipping accounts whose circuit breaker is open;
3. resolves the account **now** - decrypt key, build/reuse provider - so a key
   rotated or an account deleted mid-run is seen by the very next call;
4. keeps in-flight calls on the provider they started with: a hot-swapped key only
   affects *new* calls, and the old provider is closed once its last call returns;
5. emits a :class:`UsageRecord` for the governor/ledger, success or failure.

The pool depends on two small protocols (:class:`AccountResolver`,
:class:`UsageSink`) implemented by the account store - never on SQLAlchemy - so
tests can drive it with fakes and the orchestrator can drive it with the real
store. Error taxonomy stays :mod:`app.providers.errors`: nothing else escapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountStatus, BindingStrategy
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.core.clock import utc_now
from app.core.logging import get_logger
from app.providers.base import LLMProvider
from app.providers.errors import (
    NoAccountAvailable,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.factory import build_provider

__all__ = [
    "AccountResolver",
    "BreakerState",
    "CircuitBreaker",
    "ProviderPool",
    "ResolvedAccountLike",
    "UsageRecord",
    "UsageSink",
]

logger = get_logger(__name__)

#: Errors where trying the *next* account can still save the call. A malformed
#: response or an unsupported feature is a property of the call, not the account,
#: so those fail fast instead of burning every credential in the binding.
FAILOVER_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderAuthError,
    ProviderQuotaExceeded,
)


# --------------------------------------------------------------------------- records
@dataclass(frozen=True)
class UsageRecord:
    """One call's accounting, as the sink wants it (daily roll-up columns)."""

    account_id: str
    role: str
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    est_cost: float
    ts: datetime
    latency_ms: int
    ok: bool
    error_code: str | None = None


@runtime_checkable
class UsageSink(Protocol):
    """Consumer of usage records (the account store writes account_usage_daily)."""

    def emit(self, record: UsageRecord) -> None: ...


@runtime_checkable
class ResolvedAccountLike(Protocol):
    """What a resolver returns: a contract Account plus the plaintext key."""

    @property
    def account(self) -> Any:
        """The frozen ``Account`` contract for this row."""
        ...

    @property
    def api_key(self) -> str:
        """The decrypted provider key, valid for exactly one call."""
        ...


@runtime_checkable
class AccountResolver(Protocol):
    """The account store, seen from the pool. Ownership checks happen upstream."""

    def load_for_call(self, account_id: str) -> ResolvedAccountLike:
        """Decrypt and return one account for a single call.

        Raises:
            NoAccountAvailable: the account is gone or disabled.
        """
        ...

    def get_binding(self, user_id: str, role: str) -> AgentBinding | None:
        """The user's binding for ``role`` (``planner`` included), or None."""
        ...

    def set_account_status(
        self, account_id: str, status: AccountStatus, *, actor: str, reason: str
    ) -> None:
        """Persist a health transition (breaker tripped / recovered) and audit it."""
        ...


# --------------------------------------------------------------------------- breaker
class BreakerState(StrEnum):
    """Circuit state per account."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Breaker:
    failures: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: float = 0.0
    probe_in_flight: bool = False
    degraded_marked: bool = False


class CircuitBreaker:
    """N consecutive failures open the circuit; a periodic probe decides recovery.

    Pure state machine over an injectable clock - no I/O, no sleeps. The pool maps
    "opened" onto ``AccountStatus.DEGRADED`` in the store and lets a half-open probe
    close the circuit again (``recheck_interval_s`` apart).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recheck_interval_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._recheck = recheck_interval_s
        self._clock = clock
        self._states: dict[str, _Breaker] = {}

    def _entry(self, account_id: str) -> _Breaker:
        return self._states.setdefault(account_id, _Breaker())

    def allow(self, account_id: str) -> bool:
        """True when the account may be tried now (also claims the half-open probe)."""
        entry = self._entry(account_id)
        if entry.state is BreakerState.CLOSED:
            return True
        if entry.state is BreakerState.OPEN:
            if self._clock() - entry.opened_at >= self._recheck:
                entry.state = BreakerState.HALF_OPEN
                entry.probe_in_flight = True
                return True
            return False
        # HALF_OPEN: exactly one probe at a time.
        if entry.probe_in_flight:
            return False
        entry.probe_in_flight = True
        return True

    def record_success(self, account_id: str) -> bool:
        """Close the circuit. Returns True when an open circuit just recovered."""
        entry = self._entry(account_id)
        recovered = entry.state is not BreakerState.CLOSED or entry.failures > 0
        entry.failures = 0
        entry.state = BreakerState.CLOSED
        entry.probe_in_flight = False
        return recovered

    def record_failure(self, account_id: str) -> bool:
        """Count one failure. Returns True when this failure *opened* the circuit."""
        entry = self._entry(account_id)
        entry.failures += 1
        entry.probe_in_flight = False
        if entry.state is BreakerState.HALF_OPEN or entry.failures >= self._threshold:
            opened = entry.state is not BreakerState.OPEN
            entry.state = BreakerState.OPEN
            entry.opened_at = self._clock()
            return opened
        return False

    def state(self, account_id: str) -> BreakerState:
        return self._entry(account_id).state

    def failures(self, account_id: str) -> int:
        return self._entry(account_id).failures

    def reset(self, account_id: str) -> None:
        self._states.pop(account_id, None)


# --------------------------------------------------------------------------- pool
@dataclass
class _ProviderEntry:
    """One (account, key) provider instance, with its in-flight bookkeeping."""

    provider: LLMProvider
    key_fingerprint: str
    in_flight: int = 0
    retired: bool = field(default=False)

    @property
    def closable(self) -> bool:
        return self.retired and self.in_flight == 0


class ProviderPool:
    """Resolves accounts per call, applies strategies, breaks circuits, bills usage."""

    def __init__(
        self,
        resolver: AccountResolver,
        *,
        provider_factory: Callable[..., LLMProvider] = build_provider,
        usage_sink: UsageSink | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._resolver = resolver
        self._factory = provider_factory
        self._sink = usage_sink
        self._breaker = breaker if breaker is not None else CircuitBreaker()
        self._entries: dict[str, _ProviderEntry] = {}
        self._rr_counters: dict[tuple[str, str], int] = {}
        self._retired: list[_ProviderEntry] = []
        self._closing: set[asyncio.Task[None]] = set()
        self._closing_sync: list[_ProviderEntry] = []

    # -- public -----------------------------------------------------------
    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def generate(
        self,
        request: LLMRequest,
        *,
        role: str,
        user_id: str,
        model: str | None = None,
    ) -> LLMResponse:
        """One provider call for ``user_id``'s ``role`` binding.

        Raises:
            NoAccountAvailable: no usable account (no binding, all disabled/open).
            ProviderError: the last candidate failed (see subclasses).
        """
        binding = self._resolver.get_binding(user_id, role)
        if binding is None or not binding.is_active:
            raise NoAccountAvailable(
                f"no active binding for role {role!r}", provider=None, role=role
            )

        candidates = self._candidates(binding)
        usable = [account_id for account_id in candidates if self._breaker.allow(account_id)]
        if not usable:
            raise NoAccountAvailable(
                f"every account for role {role!r} is unavailable "
                "(disabled, deleted, or circuit open)",
                provider=None,
                role=role,
            )

        resolved_model = model or binding.model
        last_error: ProviderError | None = None
        for account_id in usable:
            try:
                resolved = self._resolver.load_for_call(account_id)
            except NoAccountAvailable as exc:
                logger.info("skipping account %s: %s", account_id, exc.message)
                last_error = exc
                continue

            started = time.monotonic()
            entry = self._provider_for(account_id, resolved)
            entry.in_flight += 1
            try:
                response = await entry.provider.generate(
                    request,
                    model=resolved_model,
                    role=role,
                    account_id=account_id,
                )
            except ProviderError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                self._on_failure(account_id, exc, actor=user_id)
                self._emit(
                    account_id=account_id,
                    role=role,
                    model=resolved_model,
                    usage=None,
                    latency_ms=latency_ms,
                    ok=False,
                    error_code=exc.code,
                )
                last_error = exc
                if isinstance(exc, FAILOVER_ERRORS):
                    continue  # next candidate can still save the call
                raise
            finally:
                entry.in_flight -= 1
                self._sweep()

            latency_ms = int((time.monotonic() - started) * 1000)
            self._on_success(account_id, actor=user_id)
            self._emit(
                account_id=account_id,
                role=role,
                model=response.model,
                usage=response.usage,
                latency_ms=latency_ms,
                ok=True,
            )
            return response

        if last_error is not None and not isinstance(last_error, NoAccountAvailable):
            raise last_error
        raise NoAccountAvailable(f"no usable account for role {role!r}", provider=None, role=role)

    def evict(self, account_id: str) -> None:
        """Forget a cached provider (account deleted or key rotated out)."""
        entry = self._entries.pop(account_id, None)
        if entry is not None:
            entry.retired = True
            self._retire(entry)
        self._breaker.reset(account_id)

    async def aclose(self) -> None:
        """Close every cached provider. Safe to call twice."""
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            entry.retired = True
        for entry in entries:
            entry.in_flight = 0  # aclose is a shutdown: nothing is in flight
            self._retire(entry)
        await self._drain()

    # -- internals --------------------------------------------------------
    def _candidates(self, binding: AgentBinding) -> list[str]:
        ids = list(binding.account_ids)
        if binding.strategy is BindingStrategy.ROUND_ROBIN:
            key = (binding.role if isinstance(binding.role, str) else binding.role.value, "rr")
            counter = self._rr_counters.get(key, 0)
            self._rr_counters[key] = counter + 1
            offset = counter % len(ids)
            return ids[offset:] + ids[:offset]
        if binding.strategy is BindingStrategy.DEDICATED:
            return ids[:1]
        # FAILOVER: the contract's "ordered candidate accounts".
        return ids

    def _provider_for(self, account_id: str, resolved: ResolvedAccountLike) -> _ProviderEntry:
        """Cached per (account, key). A rotated key builds a new provider at once;
        the old entry stays alive for in-flight calls (hot-swap)."""
        fingerprint = hashlib.sha256(resolved.api_key.encode("utf-8")).hexdigest()[:16]
        entry = self._entries.get(account_id)
        if entry is not None and entry.key_fingerprint == fingerprint and not entry.retired:
            return entry
        if entry is not None:
            entry.retired = True
            self._retire(entry)
        provider = self._factory(account=resolved.account, api_key=resolved.api_key)
        fresh = _ProviderEntry(provider=provider, key_fingerprint=fingerprint)
        self._entries[account_id] = fresh
        return fresh

    def _retire(self, entry: _ProviderEntry) -> None:
        """Park a provider for closing. In-flight ones stay parked until a sweep."""
        self._retired.append(entry)

    def _sweep(self) -> None:
        """Close retired providers whose last in-flight call has returned."""
        still: list[_ProviderEntry] = []
        for entry in self._retired:
            if entry.closable:
                self._close_later(entry)
            else:
                still.append(entry)
        self._retired = still

    def _close_later(self, entry: _ProviderEntry) -> None:
        """Schedule an aclose() on the running loop and track the task until done."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only outside asyncio
            self._closing_sync.append(entry)
            return
        task = loop.create_task(entry.provider.aclose())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _drain(self) -> None:
        for entry in self._retired:
            await entry.provider.aclose()
        self._retired = []
        for entry in self._closing_sync:
            await entry.provider.aclose()
        self._closing_sync = []
        if self._closing:
            await asyncio.gather(*self._closing)
            self._closing.clear()

    def _on_success(self, account_id: str, *, actor: str) -> None:
        recovered = self._breaker.record_success(account_id)
        if recovered:
            self._resolver.set_account_status(
                account_id, AccountStatus.ACTIVE, actor=actor, reason="circuit_recovered"
            )

    def _on_failure(self, account_id: str, exc: ProviderError, *, actor: str) -> None:
        opened = self._breaker.record_failure(account_id)
        if opened:
            logger.warning(
                "circuit opened for account %s after repeated failures (%s)",
                account_id,
                exc.code,
            )
            self._resolver.set_account_status(
                account_id, AccountStatus.DEGRADED, actor=actor, reason=f"circuit_open:{exc.code}"
            )

    def _emit(
        self,
        *,
        account_id: str,
        role: str,
        model: str,
        usage: TokenUsage | None,
        latency_ms: int,
        ok: bool,
        error_code: str | None = None,
    ) -> None:
        if self._sink is None:
            return
        tokens_in = usage.in_ if usage is not None else 0
        tokens_out = usage.out if usage is not None else 0
        tokens_cached = usage.cached if usage is not None else 0
        est_cost = 0.0
        if usage is not None:
            from app.core.pricing import estimate_cost

            est_cost = estimate_cost(model, usage)
        self._sink.emit(
            UsageRecord(
                account_id=account_id,
                role=role,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
                est_cost=est_cost,
                ts=utc_now(),
                latency_ms=latency_ms,
                ok=ok,
                error_code=error_code,
            )
        )
