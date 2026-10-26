"""The task scheduler: dependency-aware, bounded-parallel, retried, timed.

Step 9b, item 3. Guarantees:

* **Dependencies first.** A task runs only when every id in ``depends_on``
  has completed with ``ok`` (in this batch or an earlier one - completed
  tasks are idempotent and never re-run). Tasks whose dependencies failed
  are reported ``skipped``, never executed against missing inputs.
* **At most ``max_parallel`` concurrent tasks**, enforced by a semaphore.
* **Per-section exclusivity.** Two tasks touching the same section never run
  at the same time: batch selection is greedy over disjoint section sets,
  and a per-section ``asyncio.Lock`` registry (acquired in sorted order, so
  no AB-BA deadlock) enforces the invariant even under future changes. This
  composes with the article store's own locks, which guard the *apply*
  step; the scheduler's locks guard *execution*, so two writers can never
  both read version *n* and both patch against it.
* **Retry with backoff**: up to ``retries`` extra attempts (default 2) for
  provider-class failures, sleeping ``backoff_base_s * factor**attempt``
  between them. Non-provider failures (disabled role, no account, invalid
  output after the runtime's own repair) fail fast - retrying them only
  burns tokens. Note the split of duties: the agent runtime already does
  one *repair* call for malformed output; the scheduler retries *calls*.
* **Per-task timeout** via ``asyncio.wait_for`` on every attempt; a timeout
  is a retryable provider-class failure.
* **Call-time binding resolution.** The optional ``binding_check`` hook runs
  immediately before every attempt (the orchestrator passes the registry's
  ``binding_for``, which raises ``NoAccountAvailable`` when the user deleted
  or disabled the role's account since the last task). Nothing about
  accounts is ever cached here.

Results are applied through the ``apply_result`` callback on the scheduler's
own task (never inside a worker), in completion order within a wave - waves
are disjoint by section, so application order cannot change the outcome,
and the callback may safely persist after each task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from app.agents.context import RunState
from app.contracts.agent import AgentResult
from app.contracts.enums import ChangeStatus
from app.contracts.task import Task
from app.core.errors import ScholarLoopError
from app.providers.errors import ProviderError

__all__ = [
    "AgentExecutor",
    "BatchResult",
    "SchedulerConfig",
    "TaskOutcome",
    "TaskScheduler",
    "TaskStatus",
]

#: Default per-attempt ceiling for one task execution.
DEFAULT_TASK_TIMEOUT_S: Final[float] = 180.0


@runtime_checkable
class AgentExecutor(Protocol):
    """Anything that can execute one task: ``AgentRegistry`` satisfies this."""

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        """Execute ``task`` and return its ``AgentResult``."""
        ...


class TaskStatus:
    """Outcome vocabulary for one scheduled task (plain strings, JSON-ready)."""

    OK: Final = "ok"
    FAILED: Final = "failed"
    SKIPPED: Final = "skipped"


@dataclass
class TaskOutcome:
    """What happened to one task in a batch."""

    task_id: str
    status: str
    attempts: int = 0
    result: AgentResult | None = None
    error: str | None = None
    error_code: str | None = None

    @property
    def entry_status(self) -> ChangeStatus | None:
        """The ledger status of the produced entry, when there is one."""
        return None if self.result is None else self.result.entry.status

    @property
    def counts_as_completed(self) -> bool:
        """True when dependents may proceed (an ``ok`` entry exists)."""
        return self.status == TaskStatus.OK and self.entry_status is ChangeStatus.OK


@dataclass
class BatchResult:
    """All outcomes of one scheduler run, plus the updated completed set."""

    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)
    completed_ids: set[str] = field(default_factory=set)

    @property
    def results(self) -> list[AgentResult]:
        """Successful results (entry status ``ok``), in outcome order."""
        return [
            outcome.result
            for outcome in self.outcomes.values()
            if outcome.result is not None and outcome.entry_status is ChangeStatus.OK
        ]

    @property
    def any_needs_user_input(self) -> bool:
        """True when any produced entry halted for the user (HARD RULE 1)."""
        return any(
            outcome.entry_status is ChangeStatus.NEEDS_USER_INPUT
            for outcome in self.outcomes.values()
        )

    @property
    def failed_task_ids(self) -> list[str]:
        """Tasks that exhausted their attempts."""
        return [
            tid for tid, outcome in self.outcomes.items() if outcome.status == TaskStatus.FAILED
        ]


@dataclass(frozen=True)
class SchedulerConfig:
    """Knobs for one scheduler. Defaults follow the Step 9 brief."""

    max_parallel: int = 3
    retries: int = 2
    backoff_base_s: float = 0.5
    backoff_factor: float = 2.0
    task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.backoff_base_s < 0 or self.backoff_factor < 1:
            raise ValueError("backoff must be non-negative with factor >= 1")
        if self.task_timeout_s <= 0:
            raise ValueError("task_timeout_s must be positive")


def _is_retryable(exc: BaseException) -> bool:
    """Provider-class failures and timeouts retry; everything else fails fast."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, ProviderError):
        # The provider error taxonomy already classifies transience
        # (timeout/rate-limit retry; no-account/bad-response do not).
        return bool(exc.retryable)
    return False


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ScholarLoopError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "task_timeout"
    return type(exc).__name__


class TaskScheduler:
    """Executes plan tasks with dependencies, bounds, retries and timeouts."""

    def __init__(
        self,
        executor: AgentExecutor,
        *,
        config: SchedulerConfig | None = None,
        binding_check: Callable[[Task], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._executor = executor
        self._config = config or SchedulerConfig()
        self._binding_check = binding_check
        self._sleep = sleep
        self._section_locks: dict[str, asyncio.Lock] = {}

    @property
    def config(self) -> SchedulerConfig:
        """The active scheduler configuration."""
        return self._config

    # -- one task -----------------------------------------------------------
    async def _attempt(self, task: Task, run_state: RunState) -> AgentResult:
        if self._binding_check is not None:
            self._binding_check(task)  # resolves role -> binding -> account *now*
        async with asyncio.timeout(self._config.task_timeout_s):
            return await self._executor.run(task, run_state)

    async def _run_task(
        self,
        task: Task,
        *,
        run_state_factory: Callable[[], Awaitable[RunState]],
        semaphore: asyncio.Semaphore,
    ) -> TaskOutcome:
        outcome = TaskOutcome(task_id=task.id, status=TaskStatus.FAILED)
        locks = [
            self._section_locks.setdefault(sid, asyncio.Lock()) for sid in sorted(task.section_ids)
        ]
        acquired: list[asyncio.Lock] = []
        try:
            async with semaphore:
                for lock in locks:
                    await lock.acquire()
                    acquired.append(lock)
                attempts_allowed = self._config.retries + 1
                for attempt in range(attempts_allowed):
                    outcome.attempts = attempt + 1
                    try:
                        run_state = await run_state_factory()
                        result = await self._attempt(task, run_state)
                    except Exception as exc:
                        outcome.error = str(exc)[:400]
                        outcome.error_code = _error_code(exc)
                        if not _is_retryable(exc) or attempt + 1 >= attempts_allowed:
                            return outcome
                        delay = self._config.backoff_base_s * (self._config.backoff_factor**attempt)
                        if delay > 0:
                            await self._sleep(delay)
                        continue
                    outcome.status = TaskStatus.OK
                    outcome.result = result
                    outcome.error = None
                    outcome.error_code = None
                    return outcome
                return outcome
        finally:
            for lock in reversed(acquired):
                lock.release()

    # -- one batch ------------------------------------------------------------
    async def run(
        self,
        tasks: Sequence[Task],
        *,
        run_state_factory: Callable[[], Awaitable[RunState]],
        completed_ids: set[str] | None = None,
        apply_result: Callable[[Task, AgentResult], Awaitable[AgentResult | None]] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BatchResult:
        """Run every runnable task in ``tasks`` and report all outcomes.

        Waves: repeatedly collect newly-runnable tasks (dependencies inside
        ``completed_ids``), launch them with disjoint section sets under the
        parallelism cap, apply results as they land, and continue until
        nothing is runnable. Runnable-but-section-blocked tasks wait for the
        next wave; they are never starved, because every wave finishes.
        """
        completed: set[str] = set(completed_ids or set())
        batch = BatchResult(completed_ids=set(completed))
        by_id = {task.id: task for task in tasks}
        pending: dict[str, Task] = {task.id: task for task in tasks if task.id not in completed}
        for task_id in completed & by_id.keys():
            batch.outcomes[task_id] = TaskOutcome(
                task_id=task_id, status=TaskStatus.SKIPPED, error="already_completed"
            )
        semaphore = asyncio.Semaphore(self._config.max_parallel)

        while pending:
            if should_stop is not None and should_stop():
                for task_id in pending:
                    batch.outcomes[task_id] = TaskOutcome(
                        task_id=task_id, status=TaskStatus.SKIPPED, error="user_stop_requested"
                    )
                break

            ready = [task for task in pending.values() if set(task.depends_on) <= completed]
            if not ready:
                for task_id, task in pending.items():
                    missing = sorted(set(task.depends_on) - completed)
                    batch.outcomes[task_id] = TaskOutcome(
                        task_id=task_id,
                        status=TaskStatus.SKIPPED,
                        error=f"dependency_not_completed:{','.join(missing)}",
                    )
                break

            # Greedy disjoint selection, declaration order, cap = max_parallel.
            wave: list[Task] = []
            busy_sections: set[str] = set()
            for task in ready:
                if len(wave) >= self._config.max_parallel:
                    break
                sections = set(task.section_ids)
                if sections & busy_sections:
                    continue
                wave.append(task)
                busy_sections |= sections
            if not wave:  # pragma: no cover - ready non-empty always yields one
                wave = [ready[0]]

            for task in wave:
                pending.pop(task.id, None)

            async def _one(task: Task) -> TaskOutcome:
                return await self._run_task(
                    task, run_state_factory=run_state_factory, semaphore=semaphore
                )

            running = {asyncio.create_task(_one(task)): task for task in wave}
            while running:
                done, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    task = running.pop(finished)
                    outcome = finished.result()
                    batch.outcomes[task.id] = outcome
                    if outcome.result is not None and apply_result is not None:
                        await apply_result(task, outcome.result)
                    if outcome.counts_as_completed:
                        completed.add(task.id)
                        batch.completed_ids.add(task.id)

        batch.completed_ids = set(completed)
        return batch
