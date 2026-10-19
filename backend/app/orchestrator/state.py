"""Run state types: phases, the executor seam, and the outcome of a run.

The executor seam is deliberately narrow. Concrete agents (and their runtime,
skills and tools) are a separate step with an open PR; the orchestrator only
needs ``execute(task, context) -> AgentResult`` from whatever satisfies
:class:`AgentExecutor`, and it treats the result as untrusted data until it
has checked task/role ownership, applied the patches and recorded the entry
itself. The governor likewise slots in later behind the policies the
orchestrator already enforces at run level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, LoopAction
from app.contracts.loop import LoopDecision
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.orchestrator.errors import MissingExecutorError

__all__ = [
    "AgentExecutor",
    "ExecutorRegistry",
    "RunOutcome",
    "RunPhase",
    "RunStatus",
    "TaskContext",
]


class RunPhase(StrEnum):
    """The documented state machine, plus ``PAUSED`` for an ``ASK_USER`` hold."""

    INTAKE = "INTAKE"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    DECIDING = "DECIDING"
    CHECKPOINT = "CHECKPOINT"
    REPLANNING = "REPLANNING"
    DONE = "DONE"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"


class RunStatus(StrEnum):
    """How a finished ``run()`` ended - one value per terminal loop action."""

    OFFER_FINISH = "offer_finish"
    AWAITING_USER = "awaiting_user"
    STOPPED_EROSION = "stopped_erosion"
    STOPPED_BUDGET = "stopped_budget"
    STOPPED_ERRORS = "stopped_errors"
    STOPPED_USER = "stopped_user"

    @classmethod
    def from_action(cls, action: LoopAction) -> RunStatus:
        """Map a terminal (or pausing) loop action to its run status."""
        mapping = {
            LoopAction.OFFER_FINISH: cls.OFFER_FINISH,
            LoopAction.ASK_USER: cls.AWAITING_USER,
            LoopAction.STOP_EROSION: cls.STOPPED_EROSION,
            LoopAction.STOP_BUDGET: cls.STOPPED_BUDGET,
            LoopAction.STOP_ERRORS: cls.STOPPED_ERRORS,
            LoopAction.STOP_USER: cls.STOPPED_USER,
        }
        try:
            return mapping[action]
        except KeyError as exc:
            raise ValueError(f"action {action.value} does not end a run") from exc


@dataclass(frozen=True)
class TaskContext:
    """Everything an executor may need for one task, passed by reference.

    Only the task's own sections and source cards are resolved. ``digest_text``
    is the bounded ``claude.md`` the current plan was written from, so history
    reaches an agent at a constant, already-budgeted size.
    """

    brief: Brief
    task: Task
    iteration: int
    plan_version: int
    digest_text: str
    sections: tuple[SectionState, ...] = ()
    section_texts: dict[str, str] = field(default_factory=dict)
    source_cards: tuple[SourceCard, ...] = ()


class AgentExecutor(Protocol):
    """One role's task runner. Concrete agents implement exactly this."""

    async def execute(self, task: Task, context: TaskContext) -> AgentResult:
        """Execute ``task`` and return its ``AgentResult`` (entry first, always)."""
        ...


class ExecutorRegistry:
    """Role -> executor, with an explicit error for an unregistered role."""

    def __init__(self, executors: dict[AgentRole, AgentExecutor] | None = None) -> None:
        self._executors: dict[AgentRole, AgentExecutor] = dict(executors or {})

    def register(self, role: AgentRole, executor: AgentExecutor) -> ExecutorRegistry:
        """Register (or replace) the executor for ``role``."""
        self._executors[role] = executor
        return self

    def get(self, role: AgentRole) -> AgentExecutor | None:
        """The executor for ``role``, or ``None`` - a run records that as a failure."""
        return self._executors.get(role)

    def require(self, role: AgentRole) -> AgentExecutor:
        """The executor for ``role``, or :class:`MissingExecutorError`."""
        executor = self._executors.get(role)
        if executor is None:
            raise MissingExecutorError(f"no executor registered for role {role.value!r}")
        return executor

    @property
    def roles(self) -> tuple[AgentRole, ...]:
        """Roles with a registered executor."""
        return tuple(self._executors)


@dataclass(frozen=True)
class RunOutcome:
    """The auditable result of one ``Orchestrator.run`` - references, not bodies."""

    status: RunStatus
    phase: RunPhase
    iterations: int
    decisions: tuple[LoopDecision, ...]
    q_history: tuple[int, ...]
    run_tokens: int
    planner_tokens: int
    agent_tokens: int
    final_plan_version: int
    digest_text: str
    sections: tuple[SectionState, ...] = ()
    reports: tuple[QualityReport, ...] = ()
    best_snapshot_id: str | None = None
    best_q: float | None = None
    #: True when a STOP_EROSION decision restored the best snapshot.
    rolled_back: bool = False
    #: The summaries of the ``needs_user_input`` entries that paused the run.
    questions: tuple[str, ...] = ()

    @property
    def final_decision(self) -> LoopDecision:
        """The decision that ended the run (a run always ends with one)."""
        return self.decisions[-1]
