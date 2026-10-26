"""Scheduler: dependencies, parallelism cap, section exclusivity, retries,
timeouts, call-time binding checks and idempotent skips."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.context import AgentRunState, RunState
from app.contracts.agent import AgentResult
from app.contracts.enums import AgentRole, ChangeStatus
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.task import Task
from app.core.ids import new_ulid
from app.orchestrator.scheduler import SchedulerConfig, TaskScheduler, TaskStatus
from app.providers.errors import ProviderTimeout
from tests.orchestrator_support import make_task

pytestmark = pytest.mark.orchestrator


async def _run_state() -> RunState:
    return AgentRunState(iter=1)


def _result(task: Task, *, status: ChangeStatus = ChangeStatus.OK) -> AgentResult:
    return AgentResult(
        entry=ChangeEntry(
            id=new_ulid(),
            iter=1,
            task_id=task.id,
            role=task.role,
            targets=list(task.section_ids),
            summary="Done.",
            tokens=TokenUsage(in_=1, out=1, cached=0),
            status=status,
        )
    )


class RecordingExecutor:
    """Records start/finish order and peak concurrency; can fail on demand."""

    def __init__(
        self,
        *,
        failures: dict[str, int] | None = None,
        delay: float = 0.0,
        hang_tasks: set[str] | None = None,
    ) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.active = 0
        self.peak = 0
        self.section_overlap = False
        self._active_sections: set[str] = set()
        self._failures = dict(failures or {})
        self._delay = delay
        self._hang = hang_tasks or set()

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        del run_state
        self.started.append(task.id)
        if self._failures.get(task.id, 0) > 0:
            self._failures[task.id] -= 1
            raise ProviderTimeout("scripted timeout", provider="mock")
        sections = set(task.section_ids)
        if sections & self._active_sections:
            self.section_overlap = True
        self._active_sections |= sections
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if task.id in self._hang:
                await asyncio.sleep(60)
            elif self._delay:
                await asyncio.sleep(self._delay)
        finally:
            self.active -= 1
            self._active_sections -= sections
        self.finished.append(task.id)
        return _result(task)


def _scheduler(executor: RecordingExecutor, **overrides: object) -> TaskScheduler:
    config = SchedulerConfig(
        max_parallel=int(overrides.get("max_parallel", 3)),  # type: ignore[arg-type]
        retries=int(overrides.get("retries", 2)),  # type: ignore[arg-type]
        backoff_base_s=0.0,
        task_timeout_s=float(overrides.get("task_timeout_s", 5.0)),  # type: ignore[arg-type]
    )
    return TaskScheduler(executor, config=config, binding_check=overrides.get("binding_check"))  # type: ignore[arg-type]


async def test_dependencies_run_in_order_and_failure_skips_dependents() -> None:
    first = make_task(AgentRole.RESEARCHER, "Research")
    second = make_task(AgentRole.WRITER, "Write", depends_on=[first.id])
    executor = RecordingExecutor(failures={first.id: 99})
    batch = await _scheduler(executor).run([first, second], run_state_factory=_run_state)
    assert batch.outcomes[first.id].status == TaskStatus.FAILED
    assert batch.outcomes[first.id].attempts == 3  # initial + 2 retries
    assert batch.outcomes[second.id].status == TaskStatus.SKIPPED
    assert second.id not in executor.started


async def test_retry_recovers_from_transient_failure() -> None:
    task = make_task(AgentRole.WRITER, "Flaky write")
    executor = RecordingExecutor(failures={task.id: 2})
    batch = await _scheduler(executor).run([task], run_state_factory=_run_state)
    outcome = batch.outcomes[task.id]
    assert outcome.status == TaskStatus.OK
    assert outcome.attempts == 3
    assert batch.completed_ids == {task.id}


async def test_max_parallel_and_section_exclusivity() -> None:
    tasks = [
        make_task(AgentRole.WRITER, f"Write {index}", section_ids=[f"section-{index}"])
        for index in range(6)
    ]
    same_a = make_task(AgentRole.WRITER, "Same A", section_ids=["shared"])
    same_b = make_task(AgentRole.WRITER, "Same B", section_ids=["shared"])
    executor = RecordingExecutor(delay=0.05)
    batch = await _scheduler(executor, max_parallel=3).run(
        [*tasks, same_a, same_b], run_state_factory=_run_state
    )
    assert executor.peak <= 3
    assert executor.section_overlap is False
    assert all(outcome.counts_as_completed for outcome in batch.outcomes.values())


async def test_task_timeout_is_retried_then_failed() -> None:
    task = make_task(AgentRole.WRITER, "Hangs forever")
    executor = RecordingExecutor(hang_tasks={task.id})
    scheduler = _scheduler(executor, task_timeout_s=0.05, retries=1)
    batch = await scheduler.run([task], run_state_factory=_run_state)
    outcome = batch.outcomes[task.id]
    assert outcome.status == TaskStatus.FAILED
    assert outcome.attempts == 2
    assert outcome.error_code == "task_timeout"


async def test_binding_check_runs_before_every_attempt() -> None:
    task = make_task(AgentRole.WRITER, "Checked write")
    seen: list[str] = []
    executor = RecordingExecutor(failures={task.id: 1})
    scheduler = _scheduler(executor, binding_check=lambda checked: seen.append(checked.id))
    batch = await scheduler.run([task], run_state_factory=_run_state)
    assert batch.outcomes[task.id].status == TaskStatus.OK
    assert seen == [task.id, task.id]  # once per attempt, resolved at call time


async def test_completed_tasks_are_idempotent() -> None:
    done = make_task(AgentRole.RESEARCHER, "Already done")
    todo = make_task(AgentRole.WRITER, "Still todo", depends_on=[done.id])
    executor = RecordingExecutor()
    batch = await _scheduler(executor).run(
        [done, todo], run_state_factory=_run_state, completed_ids={done.id}
    )
    assert done.id not in executor.started
    assert batch.outcomes[todo.id].counts_as_completed


async def test_should_stop_skips_unstarted_tasks() -> None:
    tasks = [make_task(AgentRole.WRITER, f"T{index}") for index in range(3)]
    executor = RecordingExecutor()
    batch = await _scheduler(executor).run(
        tasks, run_state_factory=_run_state, should_stop=lambda: True
    )
    assert executor.started == []
    assert all(outcome.status == TaskStatus.SKIPPED for outcome in batch.outcomes.values())
