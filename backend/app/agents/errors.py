"""Agent-runtime errors, all carrying a machine-readable ``code``.

Validation failures are distinct from provider failures on purpose: only the former
earn a repair call. A timeout, a rate limit or a malformed provider response is the
provider's problem (or the account's), and the orchestrator handles it with retry and
failover policy - not by asking the same model to fix its JSON.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.contracts.ledger import TokenUsage
from app.core.errors import ScholarLoopError

__all__ = [
    "AgentDisabled",
    "AgentError",
    "AgentNotRegistered",
    "AgentOutputInvalid",
    "AgentRegistrationError",
    "AgentTaskMismatch",
]


class AgentError(ScholarLoopError):
    """Base class for every agent-runtime failure."""

    code = "agent_error"
    http_status = 500


class AgentTaskMismatch(AgentError):
    """An agent was handed a task owned by a different role."""

    code = "agent_task_mismatch"
    http_status = 422


class AgentOutputInvalid(AgentError):
    """Output still fails validation after the single permitted repair call.

    Carries every validation problem, the attempt count and the tokens that were
    spent (including the failed attempt). The caller sees a structured failure; it
    never sees a half-validated ``AgentResult``.
    """

    code = "agent_output_invalid"
    http_status = 502

    def __init__(
        self,
        *,
        role: str,
        task_id: str,
        attempts: int,
        errors: Sequence[str],
        tokens: TokenUsage,
    ) -> None:
        self.role = role
        self.task_id = task_id
        self.attempts = attempts
        self.errors: tuple[str, ...] = tuple(errors)
        self.tokens = tokens
        super().__init__(
            f"agent '{role}' output failed validation after {attempts} attempt(s)",
            role=role,
            task_id=task_id,
            attempts=attempts,
            errors=list(self.errors),
            tokens=tokens.model_dump(mode="json"),
        )


class AgentNotRegistered(AgentError):
    """No agent is registered for this role."""

    code = "agent_not_registered"
    http_status = 404


class AgentDisabled(AgentError):
    """The role is registered but explicitly disabled."""

    code = "agent_disabled"
    http_status = 409


class AgentRegistrationError(AgentError):
    """A registration is inconsistent (duplicate, wrong role, missing factory)."""

    code = "agent_registration_error"
    http_status = 500
