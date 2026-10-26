"""The run state machine: every transition validated, persisted and evented.

Step 9b, item 2. States::

    INTAKE -> PLANNING -> EXECUTING -> VALIDATING -> DECIDING
    DECIDING -> REPLANNING -> EXECUTING            (next iteration)
    DECIDING -> CHECKPOINT -> REPLANNING | DONE    (user decides)
    DECIDING -> DONE | STOPPED_*
    any running state -> STOPPED_USER              (user stop, always legal)
    STOPPED_* -> REPLANNING                        (user: add budget / continue)
    STOPPED_* -> DONE                              (user: rollback / finish)
    INTAKE -> EXECUTING                            (quick-edit cheap path)
    EXECUTING -> DONE | CHECKPOINT | STOPPED_ERRORS (quick-edit outcomes)

Crash safety: :meth:`StateMachine.transition` writes the record through the
:class:`RunStore` *and* appends a :class:`RunEvent` before returning, and
the orchestrator persists after every applied task result as well. A crash
therefore resumes from the last persisted state, and tasks are idempotent
by id: a task with an ``ok`` ledger entry is never executed twice (see
:func:`completed_task_ids` and the scheduler).

``CHECKPOINT`` is the single "waiting for the user" state; the record's
``checkpoint_kind`` says why (``completion``, ``needs_input``,
``evaluation_failed``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import Field

from app.contracts.common import Contract
from app.contracts.enums import ChangeStatus
from app.contracts.ledger import ChangeEntry
from app.core.clock import utc_now
from app.orchestrator.errors import InvalidTransition

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a runtime cycle
    from app.orchestrator.persistence import RunRecord, RunStore

__all__ = [
    "ALLOWED_TRANSITIONS",
    "RUNNING_STATES",
    "TERMINAL_STATES",
    "CheckpointKind",
    "RunEvent",
    "RunStateName",
    "StateMachine",
    "can_transition",
    "completed_task_ids",
]


class RunStateName(StrEnum):
    """The twelve run states (Step 9b, item 2)."""

    INTAKE = "INTAKE"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    DECIDING = "DECIDING"
    CHECKPOINT = "CHECKPOINT"
    REPLANNING = "REPLANNING"
    DONE = "DONE"
    STOPPED_EROSION = "STOPPED_EROSION"
    STOPPED_BUDGET = "STOPPED_BUDGET"
    STOPPED_ERRORS = "STOPPED_ERRORS"
    STOPPED_USER = "STOPPED_USER"


_STOPPED: Final[frozenset[RunStateName]] = frozenset(
    {
        RunStateName.STOPPED_EROSION,
        RunStateName.STOPPED_BUDGET,
        RunStateName.STOPPED_ERRORS,
        RunStateName.STOPPED_USER,
    }
)

#: States in which a run may still do work without a user decision.
RUNNING_STATES: Final[frozenset[RunStateName]] = frozenset(
    {
        RunStateName.INTAKE,
        RunStateName.PLANNING,
        RunStateName.EXECUTING,
        RunStateName.VALIDATING,
        RunStateName.DECIDING,
        RunStateName.REPLANNING,
    }
)

#: States a run never leaves on its own. ``STOPPED_*`` can still be revived
#: by an explicit user decision; ``DONE`` cannot.
TERMINAL_STATES: Final[frozenset[RunStateName]] = _STOPPED | {RunStateName.DONE}


def _user_revival() -> frozenset[RunStateName]:
    return frozenset({RunStateName.REPLANNING, RunStateName.DONE, RunStateName.STOPPED_USER})


#: The transition table. Same-state "transitions" are always allowed (they
#: are how progress events are recorded without a state change).
ALLOWED_TRANSITIONS: Final[dict[RunStateName, frozenset[RunStateName]]] = {
    RunStateName.INTAKE: frozenset(
        {RunStateName.PLANNING, RunStateName.EXECUTING, RunStateName.STOPPED_USER}
    ),
    RunStateName.PLANNING: frozenset(
        {RunStateName.EXECUTING, RunStateName.STOPPED_ERRORS, RunStateName.STOPPED_USER}
    ),
    RunStateName.EXECUTING: frozenset(
        {
            RunStateName.VALIDATING,
            RunStateName.CHECKPOINT,
            RunStateName.DONE,
            RunStateName.STOPPED_ERRORS,
            RunStateName.STOPPED_USER,
        }
    ),
    RunStateName.VALIDATING: frozenset({RunStateName.DECIDING, RunStateName.STOPPED_USER}),
    RunStateName.DECIDING: frozenset(
        {
            RunStateName.REPLANNING,
            RunStateName.CHECKPOINT,
            RunStateName.DONE,
            RunStateName.STOPPED_EROSION,
            RunStateName.STOPPED_BUDGET,
            RunStateName.STOPPED_ERRORS,
            RunStateName.STOPPED_USER,
        }
    ),
    RunStateName.CHECKPOINT: frozenset(
        {
            RunStateName.REPLANNING,
            RunStateName.EXECUTING,  # quick-edit resume after user input
            RunStateName.DONE,
            RunStateName.STOPPED_USER,
        }
    ),
    RunStateName.REPLANNING: frozenset(
        {RunStateName.EXECUTING, RunStateName.STOPPED_ERRORS, RunStateName.STOPPED_USER}
    ),
    RunStateName.DONE: frozenset(),
    RunStateName.STOPPED_EROSION: _user_revival(),
    RunStateName.STOPPED_BUDGET: _user_revival(),
    RunStateName.STOPPED_ERRORS: _user_revival(),
    RunStateName.STOPPED_USER: frozenset({RunStateName.REPLANNING, RunStateName.DONE}),
}


class CheckpointKind(StrEnum):
    """Why a run is paused in ``CHECKPOINT``."""

    COMPLETION = "completion"
    NEEDS_INPUT = "needs_input"
    EVALUATION_FAILED = "evaluation_failed"


def can_transition(source: RunStateName, target: RunStateName) -> bool:
    """True when the table allows ``source -> target`` (or they are equal)."""
    return source is target or target in ALLOWED_TRANSITIONS[source]


class RunEvent(Contract):
    """One persisted run event. The SSE stream replays these in ``seq`` order."""

    seq: int = Field(ge=0)
    ts: datetime
    type: str = Field(min_length=1, max_length=64, description="Stable snake_case event code.")
    state: RunStateName
    iter: int = Field(default=0, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


def completed_task_ids(entries: Iterable[ChangeEntry]) -> set[str]:
    """Task ids with an ``ok`` entry: done, and never re-executed (idempotent)."""
    return {entry.task_id for entry in entries if entry.status is ChangeStatus.OK}


class _RecordLike(Protocol):
    """The slice of ``RunRecord`` the state machine mutates."""

    id: str
    state: RunStateName
    iter: int
    events: list[RunEvent]
    updated_at: datetime


class StateMachine:
    """Validates transitions, stamps events and persists through the store."""

    def __init__(self, store: RunStore, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock

    @property
    def store(self) -> RunStore:
        """The persistence port every transition is written through."""
        return self._store

    def next_seq(self, record: _RecordLike) -> int:
        """Next event sequence number for ``record``."""
        return record.events[-1].seq + 1 if record.events else 0

    def emit(
        self,
        record: RunRecord,
        event_type: str,
        *,
        detail: dict[str, Any] | None = None,
        save: bool = True,
    ) -> RunEvent:
        """Append an event in the current state; persist unless batched."""
        event = RunEvent(
            seq=self.next_seq(record),
            ts=self._clock(),
            type=event_type,
            state=record.state,
            iter=record.iter,
            detail=detail or {},
        )
        record.events.append(event)
        record.updated_at = event.ts
        if save:
            self._store.save(record)
        return event

    def transition(
        self,
        record: RunRecord,
        target: RunStateName,
        *,
        event_type: str = "state_changed",
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Move ``record`` to ``target``, persist, and return the record.

        Raises:
            InvalidTransition: the table forbids this move. The record is
                left untouched - a refused transition is never half-saved.
        """
        if not can_transition(record.state, target):
            raise InvalidTransition(
                f"run {record.id}: cannot move {record.state.value} -> {target.value}",
                run_id=record.id,
                source=record.state.value,
                target=target.value,
            )
        source = record.state
        record.state = target
        merged = {"from": source.value, "to": target.value, **(detail or {})}
        self.emit(record, event_type, detail=merged, save=False)
        self._store.save(record)
        return record

    def persist(self, record: RunRecord) -> None:
        """Persist progress within a state (e.g. after each applied task)."""
        record.updated_at = self._clock()
        self._store.save(record)
