"""The projects -> runs seam: one Protocol, two implementations.

Projects need two facts that belong to runs, not to projects:

1. *is there an active run?* - the guard that makes PATCH/DELETE answer 409 instead of
   mutating a project out from under a live agent; and
2. *what have the runs done?* - ``run_count``, the latest run's state/Q and the best Q,
   which :class:`app.projects.schemas.ProjectView` exposes as derived fields.

Both are read through :class:`RunsQuery` rather than a concrete runner, for the same
reason the planner takes a context-builder Protocol (ADR-0007): the projects router
must be testable without booting an orchestrator, provider pool or account store.

* :class:`InMemoryRunsQuery` is the fake - a handful of :class:`RunFacts` records,
  used by tests and as the honest default when no run store is wired.
* :class:`StoreRunsQuery` reads the real ``RunStore`` (one JSON record per run, Step
  9b) and answers from the persisted state. It is read-only, so it can never resurrect,
  mutate or race a run: the orchestrator remains the only writer.

``summaries`` exists so a project *page* costs one pass over the run store instead of
one pass per project: :class:`app.projects.service.ProjectService` asks for every id on
the page at once, and a single project read uses the same method with one id.

"Active" means the persisted state is in
:data:`app.orchestrator.state_machine.RUNNING_STATES` - i.e. the run may still do work
without a user decision. ``CHECKPOINT`` is deliberately *not* active: the loop has
settled and is waiting for a human, and refusing an edit then would deadlock the
console.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.orchestrator.persistence import RunRecord, RunStore
from app.orchestrator.state_machine import RUNNING_STATES, RunStateName

__all__ = [
    "ActiveRun",
    "InMemoryRunsQuery",
    "RunFacts",
    "RunSummary",
    "RunsQuery",
    "StoreRunsQuery",
]


@dataclass(frozen=True)
class ActiveRun:
    """The run that blocks a project mutation, and how to name it in the 409."""

    id: str
    state: str


@dataclass(frozen=True)
class RunSummary:
    """Run facts a project view needs. All-empty for a project with no runs."""

    run_count: int = 0
    latest_run_id: str | None = None
    latest_state: str | None = None
    latest_q: int | None = None
    best_q: float | None = None


@runtime_checkable
class RunsQuery(Protocol):
    """Read-only view over one owner's runs, scoped to a project."""

    def active_run(self, owner_id: str, project_id: str) -> ActiveRun | None:
        """The newest run of ``project_id`` that is still doing work, or None."""
        ...

    def summary(self, owner_id: str, project_id: str) -> RunSummary:
        """Counts and latest/best Q for ``project_id``'s runs."""
        ...

    def summaries(self, owner_id: str, project_ids: Sequence[str]) -> dict[str, RunSummary]:
        """Run facts for several projects, in one pass.

        Projects with no runs may be omitted from the result; callers default to
        :class:`RunSummary`.
        """
        ...


@dataclass(frozen=True)
class RunFacts:
    """One fake run. ``q`` is the run's latest Q; ``best_q`` its best."""

    id: str
    owner_id: str
    project_id: str
    state: RunStateName | str = RunStateName.DONE
    q: int | None = None
    best_q: float | None = None
    updated_at: datetime | None = None

    @property
    def state_name(self) -> str:
        """The wire spelling of the state."""
        return self.state.value if isinstance(self.state, RunStateName) else str(self.state)


def _facts_summary(facts: Sequence[RunFacts]) -> RunSummary:
    """Summary over fake runs, oldest first: the last record is the latest."""
    latest = facts[-1]
    best = max(
        (value for value in (item.best_q for item in facts) if value is not None),
        default=None,
    )
    if best is None:
        best = max((item.q for item in facts if item.q is not None), default=None)
    return RunSummary(
        run_count=len(facts),
        latest_run_id=latest.id,
        latest_state=latest.state_name,
        latest_q=latest.q,
        best_q=None if best is None else float(best),
    )


def _records_summary(records: Sequence[RunRecord]) -> RunSummary:
    """Summary over persisted records; ``(updated_at, id)`` decides which is latest."""
    latest = max(records, key=lambda record: (record.updated_at, record.id))
    series = [q for record in records for q in record.q_series()]
    best = max(
        (value for value in (record.best_q for record in records) if value is not None),
        default=None,
    )
    if best is None and series:
        best = float(max(series))
    latest_series = latest.q_series()
    return RunSummary(
        run_count=len(records),
        latest_run_id=latest.id,
        latest_state=latest.state.value,
        latest_q=latest_series[-1] if latest_series else None,
        best_q=None if best is None else float(best),
    )


class InMemoryRunsQuery:
    """Deterministic fake. Later records count as newer.

    Insertion order is the run order (oldest first), which keeps the fake readable in
    tests: append a run, and it becomes the "latest" one. ``advance`` moves a run to a
    new state so a test can show the 409 clearing when the run settles.
    """

    def __init__(self, runs: Iterable[RunFacts] = ()) -> None:
        self._runs: list[RunFacts] = []
        for facts in runs:
            self.add(facts)

    def add(self, facts: RunFacts) -> RunFacts:
        """Append a run (newest wins for ``latest_*``)."""
        self._runs.append(facts)
        return facts

    def advance(self, run_id: str, state: RunStateName | str) -> None:
        """Move one run to a new state, keeping its Q history."""
        self._runs = [
            replace(facts, state=state) if facts.id == run_id else facts for facts in self._runs
        ]

    def clear(self) -> None:
        """Forget every run (the app's default wiring starts empty)."""
        self._runs.clear()

    @property
    def runs(self) -> list[RunFacts]:
        """A copy of the fake's records, oldest first."""
        return list(self._runs)

    def _matching(self, owner_id: str, project_id: str) -> list[RunFacts]:
        return [
            facts
            for facts in self._runs
            if facts.owner_id == owner_id and facts.project_id == project_id
        ]

    def active_run(self, owner_id: str, project_id: str) -> ActiveRun | None:
        """The newest running record, or None."""
        for facts in reversed(self._matching(owner_id, project_id)):
            if facts.state in RUNNING_STATES:
                return ActiveRun(id=facts.id, state=facts.state_name)
        return None

    def summary(self, owner_id: str, project_id: str) -> RunSummary:
        """Counts and Q facts over one project's records."""
        matching = self._matching(owner_id, project_id)
        return _facts_summary(matching) if matching else RunSummary()

    def summaries(self, owner_id: str, project_ids: Sequence[str]) -> dict[str, RunSummary]:
        """One pass over the fake's records, grouped by project."""
        wanted = set(project_ids)
        grouped: dict[str, list[RunFacts]] = {}
        for facts in self._runs:
            if facts.owner_id == owner_id and facts.project_id in wanted:
                grouped.setdefault(facts.project_id, []).append(facts)
        return {project_id: _facts_summary(items) for project_id, items in grouped.items()}


class StoreRunsQuery:
    """``RunsQuery`` over the real ``RunStore`` (Step 9b persistence).

    It holds no cache: a run write is visible to the very next project read, which is
    what makes the 409 guard trustworthy. ``summaries`` loads the stored records once
    and groups them, so a page of projects costs one pass, not one pass per project.
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store

    @property
    def store(self) -> RunStore:
        """The store this view reads (used to detect a re-wired app)."""
        return self._store

    def _matching(self, owner_id: str, project_id: str) -> list[RunRecord]:
        return [
            record
            for record in self._records()
            if record.owner_id == owner_id and record.project_id == project_id
        ]

    def _records(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        for run_id in self._store.list_ids():
            record = self._store.get(run_id)
            if record is not None:
                records.append(record)
        return records

    def active_run(self, owner_id: str, project_id: str) -> ActiveRun | None:
        """The newest record whose state is still running."""
        active = [
            record
            for record in self._matching(owner_id, project_id)
            if record.state in RUNNING_STATES
        ]
        if not active:
            return None
        newest = max(active, key=lambda record: (record.updated_at, record.id))
        return ActiveRun(id=newest.id, state=newest.state.value)

    def summary(self, owner_id: str, project_id: str) -> RunSummary:
        """Counts and Q facts over one project's persisted records."""
        return self.summaries(owner_id, [project_id]).get(project_id, RunSummary())

    def summaries(self, owner_id: str, project_ids: Sequence[str]) -> dict[str, RunSummary]:
        """One pass over the run store, grouped by project id."""
        wanted = set(project_ids)
        grouped: dict[str, list[RunRecord]] = {}
        for record in self._records():
            if record.owner_id == owner_id and record.project_id in wanted:
                grouped.setdefault(record.project_id, []).append(record)
        return {project_id: _records_summary(items) for project_id, items in grouped.items()}
