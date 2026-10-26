"""The deterministic orchestrator: intake, loop control, scheduling, runs.

Step 9. The orchestrator is plain Python - never an LLM. It validates the
brief, asks the Planner for plans, dispatches tasks to agents through the
scheduler, applies results to the article/ledger, and obeys the pure loop
controller's decisions (completion, erosion, budget, errors, user).

Modules:

* :mod:`app.orchestrator.loop_controller` - pure ``evaluate(history)`` and
  the eleven detectors (Step 9a);
* :mod:`app.orchestrator.stop` - ``StopReport`` and ``on_complete`` (9a);
* :mod:`app.orchestrator.intake` - deterministic brief/quick-edit intake;
* :mod:`app.orchestrator.state_machine` - run states and transitions;
* :mod:`app.orchestrator.persistence` - the crash-resume ``RunRecord``;
* :mod:`app.orchestrator.scheduler` - dependency-aware bounded execution;
* :mod:`app.orchestrator.runner` - the :class:`Orchestrator` loop itself.
"""

from __future__ import annotations

from app.orchestrator.errors import (
    IntakeNeedsInput,
    InvalidTransition,
    OrchestratorError,
    RunConflict,
    RunNotFound,
)
from app.orchestrator.intake import (
    BRIEF_REQUIRED_FIELDS,
    IntakeKind,
    IntakeResult,
    IntakeStatus,
    QuickEditRequest,
    build_quick_edit_task,
    validate_brief,
    validate_payload,
    validate_quick_edit,
)
from app.orchestrator.loop_controller import (
    HARD_DETECTORS,
    SOFT_DETECTORS,
    DetectorHit,
    IterationRecord,
    LoopController,
    LoopHistory,
    evaluate,
    next_strikes,
)
from app.orchestrator.persistence import (
    ArticleDump,
    FileRunStore,
    InMemoryRunStore,
    RunRecord,
    RunStore,
    RunStoreError,
)
from app.orchestrator.runner import (
    Orchestrator,
    OrchestratorConfig,
    PlannerPort,
    PlannerServiceAdapter,
    RunManager,
)
from app.orchestrator.scheduler import (
    AgentExecutor,
    BatchResult,
    SchedulerConfig,
    TaskOutcome,
    TaskScheduler,
    TaskStatus,
)
from app.orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    RUNNING_STATES,
    TERMINAL_STATES,
    CheckpointKind,
    RunEvent,
    RunStateName,
    StateMachine,
    can_transition,
    completed_task_ids,
)
from app.orchestrator.stop import (
    DEFAULT_ON_COMPLETE,
    OnComplete,
    StopOption,
    StopReport,
    UserDecision,
    build_stop_report,
    options_for,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BRIEF_REQUIRED_FIELDS",
    "DEFAULT_ON_COMPLETE",
    "HARD_DETECTORS",
    "RUNNING_STATES",
    "SOFT_DETECTORS",
    "TERMINAL_STATES",
    "AgentExecutor",
    "ArticleDump",
    "BatchResult",
    "CheckpointKind",
    "DetectorHit",
    "FileRunStore",
    "InMemoryRunStore",
    "IntakeKind",
    "IntakeNeedsInput",
    "IntakeResult",
    "IntakeStatus",
    "InvalidTransition",
    "IterationRecord",
    "LoopController",
    "LoopHistory",
    "OnComplete",
    "Orchestrator",
    "OrchestratorConfig",
    "OrchestratorError",
    "PlannerPort",
    "PlannerServiceAdapter",
    "QuickEditRequest",
    "RunConflict",
    "RunEvent",
    "RunManager",
    "RunNotFound",
    "RunRecord",
    "RunStateName",
    "RunStore",
    "RunStoreError",
    "SchedulerConfig",
    "StateMachine",
    "StopOption",
    "StopReport",
    "TaskOutcome",
    "TaskScheduler",
    "TaskStatus",
    "UserDecision",
    "build_quick_edit_task",
    "build_stop_report",
    "can_transition",
    "completed_task_ids",
    "evaluate",
    "next_strikes",
    "options_for",
    "validate_brief",
    "validate_payload",
    "validate_quick_edit",
]
