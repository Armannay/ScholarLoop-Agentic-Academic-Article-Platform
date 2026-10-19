"""The orchestrator: the deterministic state machine that runs the loop.

``INTAKE -> PLANNING -> EXECUTING -> VALIDATING -> CHECKPOINT -> DECIDING``,
then ``REPLANNING``, ``DONE``, ``PAUSED`` (``ASK_USER``) or ``STOPPED``. The
:class:`LoopController` is pure arithmetic over policies; the
:class:`Orchestrator` owns the side effects - patches into the article store,
entries into the ledger, snapshots for rollback, and the bounded digest the
Planner replans from. Concrete agents plug in behind
:class:`AgentExecutor`; no LLM sits in the control path (ADR-0001).
"""

from __future__ import annotations

from app.orchestrator.controller import (
    LoopController,
    LoopState,
    churn_detected,
    detect_erosion,
    diminishing_detected,
    oscillation_detected,
    plateau_detected,
    regression_detected,
)
from app.orchestrator.errors import (
    InvalidRunStateError,
    MissingExecutorError,
    OrchestratorError,
)
from app.orchestrator.run import Orchestrator
from app.orchestrator.state import (
    AgentExecutor,
    ExecutorRegistry,
    RunOutcome,
    RunPhase,
    RunStatus,
    TaskContext,
)

__all__ = [
    "AgentExecutor",
    "ExecutorRegistry",
    "InvalidRunStateError",
    "LoopController",
    "LoopState",
    "MissingExecutorError",
    "Orchestrator",
    "OrchestratorError",
    "RunOutcome",
    "RunPhase",
    "RunStatus",
    "TaskContext",
    "churn_detected",
    "detect_erosion",
    "diminishing_detected",
    "oscillation_detected",
    "plateau_detected",
    "regression_detected",
]
