"""Planner errors - failures of the planning-only LLM boundary.

The Planner is an LLM, so its failures are expected (non-bug) failures: malformed
JSON, a plan that violates the role-distribution rule, a digest that arrives over
budget. Each carries a machine-readable ``code`` like every other
:class:`~app.core.errors.ScholarLoopError`, so the orchestrator can turn a planner
failure into a ``LoopDecision`` instead of a traceback.
"""

from __future__ import annotations

from typing import ClassVar

from app.core.errors import ScholarLoopError

__all__ = ["PlannerError", "PlannerOutputError", "PlannerProviderNotAllowed"]


class PlannerError(ScholarLoopError):
    """Base class for every planner-boundary failure."""

    code: ClassVar[str] = "planner_error"
    http_status: ClassVar[int] = 502


class PlannerOutputError(PlannerError):
    """The model answered, but the answer is not a usable Plan/Verdict.

    Raised after the single bounded repair retry is exhausted: malformed JSON, a
    contract violation, a missing required role, an invented section id, or task
    budgets that exceed the iteration cap.
    """

    code = "planner_bad_output"
    http_status = 502


class PlannerProviderNotAllowed(PlannerError):
    """The planner was handed a provider it must never use.

    The Planner runs on an Anthropic Console API key (or the mock in tests) -
    never on an OpenAI-compatible account and never on a Pro/Max subscription
    login. The account store enforces the same rule at binding time; this is
    defence in depth at the point of use.
    """

    code = "planner_provider_not_allowed"
    http_status = 409
