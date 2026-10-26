"""The orchestrator error family. All carry HTTP mappings via ScholarLoopError."""

from __future__ import annotations

from app.core.errors import ConflictError, NeedsUserInput, NotFoundError, ScholarLoopError
from app.orchestrator.intake import IntakeResult

__all__ = [
    "IntakeNeedsInput",
    "InvalidTransition",
    "OrchestratorError",
    "RunConflict",
    "RunNotFound",
]


class OrchestratorError(ScholarLoopError):
    """Base for run-orchestration failures."""

    code = "orchestrator_error"
    http_status = 500


class RunNotFound(NotFoundError):
    """No run with that id (or it belongs to another owner)."""

    code = "run_not_found"


class RunConflict(ConflictError):
    """The run exists but is in a state that refuses this operation."""

    code = "run_conflict"


class InvalidTransition(RunConflict):
    """A state-machine transition that the transition table forbids."""

    code = "invalid_transition"


class IntakeNeedsInput(NeedsUserInput):
    """Intake rejected the payload; ``result`` names the missing fields."""

    code = "needs_user_input"

    def __init__(self, result: IntakeResult) -> None:
        self.result = result
        super().__init__(
            "intake needs user input: " + ", ".join(result.missing_fields or result.problems),
            missing_fields=result.missing_fields,
            problems=result.problems,
            kind=result.kind.value,
        )
