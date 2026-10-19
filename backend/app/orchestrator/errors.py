"""Orchestrator errors - run-wiring failures, distinct from agent failures.

An *agent* failing is data (a ``failed`` ledger entry the controller counts);
these errors mean the run could not be assembled at all: no executor for a
role the caller promised, a second ``run()`` on a spent orchestrator, or an
article with no sections and no rubric to create them from.
"""

from __future__ import annotations

from typing import ClassVar

from app.core.errors import ScholarLoopError

__all__ = ["InvalidRunStateError", "MissingExecutorError", "OrchestratorError"]


class OrchestratorError(ScholarLoopError):
    """Base class for run-assembly failures."""

    code: ClassVar[str] = "orchestrator_error"
    http_status: ClassVar[int] = 500


class MissingExecutorError(OrchestratorError):
    """No executor is registered for a task's role.

    Surfaced when an ``Orchestrator`` is constructed from a partial registry in
    strict contexts; during a run the same condition is recorded as a failed
    ledger entry instead, so the loop controller stays the single judge.
    """

    code = "missing_executor"
    http_status = 500


class InvalidRunStateError(OrchestratorError):
    """The run was driven out of order (e.g. ``run()`` twice on one instance)."""

    code = "invalid_run_state"
    http_status = 409
