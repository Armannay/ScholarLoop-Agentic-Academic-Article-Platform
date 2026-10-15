"""Exceptions shared by the whole application.

Only :class:`ScholarLoopError` subclasses are raised across module boundaries;
each carries a machine-readable ``code`` so the API layer can map it to a status
without string matching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps pydantic out of the import graph
    from pydantic import ValidationError


class ScholarLoopError(Exception):
    """Base class for every expected (non-bug) failure."""

    #: Machine-readable error code. A subclass overrides it with a new literal.
    code: ClassVar[str] = "scholarloop_error"
    #: HTTP status the API maps this error to.
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def to_dict(self) -> dict[str, Any]:
        """Serialise for logs and API error bodies. Never contains secrets."""
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigError(ScholarLoopError):
    """A config file (policies, rubric, project overrides) is missing or invalid."""

    code = "config_error"
    http_status = 500


class PolicyError(ConfigError):
    """config/policies.yaml (or a project override) failed validation."""

    code = "policy_error"


class RubricError(ConfigError):
    """config/rubric.yaml failed validation."""

    code = "rubric_error"


class ContractViolation(ScholarLoopError):
    """A payload crossed a contract boundary in a shape the contract forbids."""

    code = "contract_violation"
    http_status = 422


class NotFoundError(ScholarLoopError):
    """A referenced object (section, source, task, account) does not exist."""

    code = "not_found"
    http_status = 404


class BudgetExceeded(ScholarLoopError):
    """A token or cost budget would be exceeded by the requested call."""

    code = "budget_exceeded"
    http_status = 429


class NeedsUserInput(ScholarLoopError):
    """The loop cannot proceed without a human decision (HARD RULE 1)."""

    code = "needs_user_input"
    http_status = 409


def format_validation_errors(exc: ValidationError, *, limit: int = 4) -> str:
    """One-line summary of the first ``limit`` validation errors.

    A config error that says only "invalid policies" forces a human to re-run with a
    debugger; naming the offending keys turns a five-minute hunt into a one-line fix.
    """
    errors = exc.errors(include_url=False)
    parts: list[str] = []
    for error in errors[:limit]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "<document>"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    summary = "; ".join(parts)
    remaining = len(errors) - len(parts)
    if remaining > 0:
        summary += f" (+{remaining} more)"
    return summary
