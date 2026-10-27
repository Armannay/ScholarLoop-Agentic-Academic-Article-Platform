"""Role-agent errors: a task a role may not execute, or a role wired without inputs."""

from __future__ import annotations

from app.agents.errors import AgentError

__all__ = ["RoleTaskInvalid"]


class RoleTaskInvalid(AgentError):
    """The task is well-formed but violates this role's scope.

    The writer owns exactly one section per task; the reviewer reads dirty sections
    only. Violations are refused *before* a provider call - an out-of-scope task is a
    planning error, and paying for a call to discover it would be waste.
    """

    code = "role_task_invalid"
    http_status = 422
