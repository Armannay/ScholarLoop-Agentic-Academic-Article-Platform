"""Per-section async locks so parallel agents can edit different sections.

Two properties matter and both are enforced here rather than by convention:

* **Bounded waiting.** Every acquisition has a timeout. A stuck agent surfaces as
  :class:`LockTimeout` (which the loop controller counts as an error) instead of
  hanging the iteration until the run budget expires.
* **Deadlock safety.** Multi-section acquisitions are always taken in one global
  order (:func:`order_section_ids`, lexicographic by section id), so the classic
  AB-BA cycle cannot form, however the orchestrator schedules tasks. Re-entering a
  lock the current task already holds is rejected immediately instead of timing out,
  because that is a programming error, not contention.

Locks are per section, never per article: the whole point of independently
versioned sections is that a writer on ``methods`` never waits for a writer on
``introduction``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, TypeVar

from app.core.errors import ScholarLoopError

__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "LockReentrancy",
    "LockStats",
    "LockTimeout",
    "SectionLocks",
    "order_section_ids",
]

#: Default ceiling for one acquisition. Comfortably above a normal provider call's
#: worst case, far below an iteration token cap's patience.
DEFAULT_LOCK_TIMEOUT: Final[float] = 10.0

T = TypeVar("T")


class LockTimeout(ScholarLoopError):
    """A section lock could not be acquired within the deadline."""

    code = "lock_timeout"
    http_status = 409

    def __init__(self, *, section_id: str, timeout: float, waited: float) -> None:
        self.section_id = section_id
        self.timeout = timeout
        self.waited = waited
        super().__init__(
            f"timed out after {waited:.3f}s waiting for the lock on section '{section_id}' "
            f"(limit {timeout:.3f}s)",
            section_id=section_id,
            timeout=timeout,
            waited=round(waited, 6),
        )


class LockReentrancy(ScholarLoopError):
    """The current task tried to lock a section it already holds.

    ``asyncio.Lock`` is not reentrant, so this would otherwise block until the
    timeout and look like contention. Failing fast names the real bug.
    """

    code = "lock_reentrancy"
    http_status = 500

    def __init__(self, *, section_id: str) -> None:
        self.section_id = section_id
        super().__init__(
            f"section '{section_id}' is already locked by this task; locks are not reentrant",
            section_id=section_id,
        )


def order_section_ids(section_ids: Iterable[str]) -> list[str]:
    """The one global acquisition order: sorted, de-duplicated.

    Every multi-section caller must go through this. Any two tasks that want the
    same set of sections then request them in the same order, which is what makes
    AB-BA deadlock structurally impossible.
    """
    return sorted(set(section_ids))


@dataclass(frozen=True)
class LockStats:
    """Contention counters, for logs and for the loop controller's error budget."""

    acquisitions: int = 0
    timeouts: int = 0
    reentrancy_errors: int = 0
    max_wait_seconds: float = 0.0


class _SectionLock:
    """One ``asyncio.Lock`` plus the ownership bookkeeping reentrancy needs."""

    __slots__ = ("_lock", "owner", "section_id")

    def __init__(self, section_id: str) -> None:
        self.section_id = section_id
        self._lock = asyncio.Lock()
        self.owner: asyncio.Task[object] | None = None

    @property
    def locked(self) -> bool:
        """True while any task holds this section."""
        return self._lock.locked()

    async def acquire(self, owner: asyncio.Task[object] | None) -> None:
        """Take the lock and record ``owner`` as the holder.

        The owner is passed in rather than read here: ``asyncio.wait_for`` runs this
        coroutine in a task of its own, so ``asyncio.current_task()`` would name the
        wrapper instead of the caller and break both reentrancy detection and
        :meth:`SectionLocks.owner_id`.
        """
        await self._lock.acquire()
        self.owner = owner

    def release(self) -> None:
        """Drop the lock and forget the owner."""
        self.owner = None
        self._lock.release()


class SectionLocks:
    """A registry of per-section locks with timeouts and a safe multi-lock order."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self._timeout = timeout
        self._clock = clock
        self._locks: dict[str, _SectionLock] = {}
        self._acquisitions = 0
        self._timeouts = 0
        self._reentrancy_errors = 0
        self._max_wait = 0.0

    @property
    def timeout(self) -> float:
        """Default acquisition deadline, in seconds."""
        return self._timeout

    @asynccontextmanager
    async def hold(
        self,
        section_id: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-section budgets, with diagnostics
    ) -> AsyncIterator[None]:
        """Hold one section's lock for the duration of the block.

        ``timeout=None`` falls back to the manager default; pass an explicit value to
        tighten it for a short critical section. ``asyncio.timeout`` cannot express a
        per-section budget that turns into a :class:`LockTimeout` naming the section.
        """
        budget = self._timeout if timeout is None else timeout
        lock = await self._take(section_id, deadline=self._clock() + budget, budget=budget)
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def hold_many(
        self,
        section_ids: Iterable[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - one shared deadline per batch
    ) -> AsyncIterator[None]:
        """Hold several sections at once, always in :func:`order_section_ids` order.

        The deadline is shared across the whole batch, so a five-section batch cannot
        wait five times the timeout. On timeout every already-held lock is released.
        """
        budget = self._timeout if timeout is None else timeout
        deadline = self._clock() + budget
        held: list[_SectionLock] = []
        try:
            for section_id in order_section_ids(section_ids):
                held.append(await self._take(section_id, deadline=deadline, budget=budget))
            yield
        finally:
            self._release_all(held)

    async def run_locked(self, section_ids: Iterable[str], action: Callable[[], Awaitable[T]]) -> T:
        """Run ``action`` with ``section_ids`` held. Convenience for the orchestrator."""
        async with self.hold_many(section_ids):
            return await action()

    def is_locked(self, section_id: str) -> bool:
        """True while ``section_id`` is held by some task."""
        lock = self._locks.get(section_id)
        return lock is not None and lock.locked

    def owner_id(self, section_id: str) -> int | None:
        """``id()`` of the task holding the section, for diagnostics."""
        lock = self._locks.get(section_id)
        if lock is None or lock.owner is None:
            return None
        return id(lock.owner)

    def stats(self) -> LockStats:
        """Counters since construction (or the last :meth:`reset_stats`)."""
        return LockStats(
            acquisitions=self._acquisitions,
            timeouts=self._timeouts,
            reentrancy_errors=self._reentrancy_errors,
            max_wait_seconds=self._max_wait,
        )

    def reset_stats(self) -> None:
        """Zero the counters. Tests and per-iteration reporting use this."""
        self._acquisitions = 0
        self._timeouts = 0
        self._reentrancy_errors = 0
        self._max_wait = 0.0

    def _lock_for(self, section_id: str) -> _SectionLock:
        lock = self._locks.get(section_id)
        if lock is None:
            lock = _SectionLock(section_id)
            self._locks[section_id] = lock
        return lock

    def _check_reentrancy(self, section_id: str) -> None:
        lock = self._locks.get(section_id)
        if lock is not None and lock.owner is not None and lock.owner is asyncio.current_task():
            self._reentrancy_errors += 1
            raise LockReentrancy(section_id=section_id)

    async def _take(self, section_id: str, *, deadline: float, budget: float) -> _SectionLock:
        """Acquire one section before ``deadline``, or raise LockTimeout.

        ``deadline`` is absolute on the manager's clock, which is what lets a batch
        share one budget instead of multiplying it by the number of sections.
        """
        self._check_reentrancy(section_id)
        lock = self._lock_for(section_id)
        owner = asyncio.current_task()
        started = self._clock()
        remaining = deadline - started
        if remaining <= 0:
            self._timeouts += 1
            raise LockTimeout(section_id=section_id, timeout=budget, waited=self._clock() - started)
        try:
            await asyncio.wait_for(lock.acquire(owner), timeout=remaining)
        except TimeoutError as exc:
            self._timeouts += 1
            raise LockTimeout(
                section_id=section_id, timeout=budget, waited=self._clock() - started
            ) from exc
        waited = self._clock() - started
        self._acquisitions += 1
        self._max_wait = max(self._max_wait, waited)
        return lock

    @staticmethod
    def _release_all(locks: list[_SectionLock]) -> None:
        """Release in reverse acquisition order."""
        for lock in reversed(locks):
            lock.release()
