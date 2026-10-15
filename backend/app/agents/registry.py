"""Role-to-agent registry with explicit enablement and call-time binding lookup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.agents.base import Agent, AgentBindingResolver
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.enums import AgentRole
from app.contracts.task import Task

__all__ = [
    "AgentBindingResolverUnavailable",
    "AgentDisabledError",
    "AgentRegistry",
    "UnknownAgentRoleError",
]


class AgentRegistryError(RuntimeError):
    """Base class for registry lookup and enablement failures."""


class UnknownAgentRoleError(AgentRegistryError, ValueError):
    """Raised when no class is registered for a role or the role is not in CONTRACTS."""


class AgentDisabledError(AgentRegistryError):
    """Raised when a known role has been disabled."""


class AgentBindingResolverUnavailable(AgentRegistryError):
    """Raised if the registry is asked for a binding without a resolver configured."""


def _role(role: AgentRole | str) -> AgentRole:
    """Convert supported role spellings, raising a public explicit error otherwise."""
    if isinstance(role, AgentRole):
        return role
    try:
        return AgentRole(role)
    except ValueError as exc:
        raise UnknownAgentRoleError(f"unknown agent role {role!r}") from exc


class AgentRegistry:
    """Map task roles to agent classes and resolve the latest binding for every call.

    The registry deliberately stores classes, not agent instances or bindings. Concrete
    Step 7 agents can be registered as they are implemented; projects can enable and
    disable only roles that are registered here.
    """

    def __init__(
        self,
        agent_classes: Mapping[AgentRole | str, type[Agent]] | None = None,
        *,
        binding_resolver: AgentBindingResolver | None = None,
    ) -> None:
        self._classes: dict[AgentRole, type[Agent]] = {}
        self._enabled: dict[AgentRole, bool] = {}
        self._binding_resolver = binding_resolver
        if agent_classes is not None:
            for role, agent_class in agent_classes.items():
                self.register(role, agent_class)

    def register(
        self,
        role: AgentRole | str,
        agent_class: type[Agent],
        *,
        enabled: bool = True,
    ) -> None:
        """Register one role class; duplicate registrations require an explicit replace."""
        resolved = _role(role)
        if not isinstance(agent_class, type) or not issubclass(agent_class, Agent):
            raise TypeError("agent_class must be a subclass of Agent")
        if resolved in self._classes:
            raise ValueError(f"an agent class is already registered for role {resolved.value!r}")
        self._classes[resolved] = agent_class
        self._enabled[resolved] = enabled

    def enable(self, role: AgentRole | str) -> None:
        """Enable a registered role; unknown roles fail explicitly."""
        resolved = self._registered_role(role)
        self._enabled[resolved] = True

    def disable(self, role: AgentRole | str) -> None:
        """Disable a registered role; unknown roles fail explicitly."""
        resolved = self._registered_role(role)
        self._enabled[resolved] = False

    def resolve(self, role: AgentRole | str) -> type[Agent]:
        """Return the enabled agent class for ``role``."""
        resolved = self._registered_role(role)
        if not self._enabled[resolved]:
            raise AgentDisabledError(f"agent role {resolved.value!r} is disabled")
        return self._classes[resolved]

    def create(self, role: AgentRole | str, **dependencies: Any) -> Agent:
        """Create a fresh agent instance; no provider binding is captured here."""
        resolved = _role(role)
        agent_class = self.resolve(resolved)
        kwargs = dict(dependencies)
        if kwargs.get("role") is None:
            kwargs["role"] = resolved
        if self._binding_resolver is not None and kwargs.get("binding_resolver") is None:
            kwargs["binding_resolver"] = self
        return agent_class(**kwargs)

    async def run(self, task: Task, run_state: object, **dependencies: Any) -> AgentResult:
        """Create the registered agent for a task and execute it."""
        return await self.create(task.role, **dependencies).run(task, run_state)

    def resolve_binding(self, role: str) -> AgentBinding:
        """Delegate every lookup; the binding is never retained between provider calls."""
        resolved = self._registered_role(role)
        if not self._enabled[resolved]:
            raise AgentDisabledError(f"agent role {resolved.value!r} is disabled")
        if self._binding_resolver is None:
            raise AgentBindingResolverUnavailable("no call-time binding resolver is configured")
        return self._binding_resolver.resolve_binding(resolved.value)

    @property
    def enabled_roles(self) -> frozenset[AgentRole]:
        """Registered roles currently available for task execution."""
        return frozenset(role for role, enabled in self._enabled.items() if enabled)

    def _registered_role(self, role: AgentRole | str) -> AgentRole:
        resolved = _role(role)
        if resolved not in self._classes:
            raise UnknownAgentRoleError(f"no agent class is registered for role {resolved.value!r}")
        return resolved
