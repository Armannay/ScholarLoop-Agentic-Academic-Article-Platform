"""Role -> agent registry with explicit enable/disable and call-time binding checks.

The registry is intentionally tiny. It is the single place that answers three
questions for the orchestrator:

* which agent executes this role (Step 7 registers the concrete classes);
* is that role enabled right now (a disabled role must fail, not silently no-op);
* does the role have an active binding right now (resolved at call time, so a deleted
  or disabled account is visible immediately).

Registration is deterministic: a role can be registered once, factories are called at
most once (the instance is cached), and every lookup returns either the agent or a
typed error - never ``None``. A disabled role is checked *before* its factory runs, so
disabling can prevent work from even being constructed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.agents.base import Agent, BindingResolver
from app.agents.context import RunState
from app.agents.errors import (
    AgentDisabled,
    AgentNotRegistered,
    AgentRegistrationError,
)
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.enums import AgentRole, BindingRole
from app.contracts.task import Task
from app.providers.errors import NoAccountAvailable

__all__ = ["AgentFactory", "AgentRegistry"]

#: Builds the agent for a role. Called lazily, at most once per registration.
AgentFactory = Callable[[], Agent]


@dataclass
class _Registration:
    """One role's factory, cached instance and enabled flag."""

    factory: AgentFactory | None = None
    agent: Agent | None = None
    enabled: bool = True
    built: bool = field(default=False)


class AgentRegistry:
    """Map roles to agents; enforce enablement and binding presence at dispatch."""

    def __init__(self, *, bindings: BindingResolver) -> None:
        self._bindings = bindings
        self._registrations: dict[AgentRole, _Registration] = {}

    # -- registration -----------------------------------------------------
    def register(self, role: AgentRole, factory: AgentFactory, *, enabled: bool = True) -> None:
        """Register ``factory`` for ``role``. A role may be registered once."""
        if not isinstance(role, AgentRole):
            raise AgentRegistrationError(f"role must be an AgentRole, got {role!r}")
        if role in self._registrations:
            raise AgentRegistrationError(f"role '{role.value}' is already registered")
        if not callable(factory):
            raise AgentRegistrationError(f"factory for role '{role.value}' is not callable")
        self._registrations[role] = _Registration(factory=factory, enabled=enabled)

    def register_agent(self, agent: Agent, *, enabled: bool = True) -> None:
        """Register an already-built agent (its ``role`` decides the key)."""
        if not isinstance(agent, Agent):
            raise AgentRegistrationError(f"expected an Agent instance, got {type(agent).__name__}")
        self.register(agent.role, lambda: agent, enabled=enabled)

    def build_all(self) -> None:
        """Instantiate every enabled, not-yet-built registration now.

        Optional: useful at startup to surface construction errors early. Disabled
        roles are not built.
        """
        for role in self.registered_roles():
            if self.is_enabled(role):
                _ = self.resolve(role)

    # -- enable / disable -------------------------------------------------
    def enable(self, role: AgentRole) -> None:
        """Allow ``role`` to execute. Unknown roles are still an error."""
        self._registration(role).enabled = True

    def disable(self, role: AgentRole) -> None:
        """Prevent ``role`` from executing (and from being built)."""
        self._registration(role).enabled = False

    def set_enabled(self, role: AgentRole, enabled: bool) -> None:
        """Set the flag explicitly, for config-driven wiring."""
        self._registration(role).enabled = enabled

    def is_enabled(self, role: AgentRole) -> bool:
        """True when the role is registered and enabled."""
        registration = self._registrations.get(role)
        return registration is not None and registration.enabled

    def registered_roles(self) -> tuple[AgentRole, ...]:
        """Registered roles, in enum order (deterministic)."""
        return tuple(role for role in AgentRole if role in self._registrations)

    def _registration(self, role: AgentRole) -> _Registration:
        registration = self._registrations.get(role)
        if registration is None:
            known = ", ".join(role.value for role in self.registered_roles()) or "(none)"
            raise AgentNotRegistered(
                f"no agent registered for role '{role.value}'; registered: {known}",
                role=role.value,
            )
        return registration

    # -- resolution -------------------------------------------------------
    def binding_for(self, role: AgentRole) -> AgentBinding:
        """Resolve the role's binding now, or raise ``NoAccountAvailable``.

        An unregistered role is an error here too: asking for the binding of a role
        the registry cannot dispatch is a wiring bug, not a "user has no account".
        """
        _ = self._registration(role)
        binding = self._bindings.resolve(BindingRole(role.value))
        if binding is None or not binding.is_active:
            raise NoAccountAvailable(
                f"no active binding for role '{role.value}'",
                provider=None,
                role=role.value,
            )
        return binding

    def resolve(self, role: AgentRole) -> Agent:
        """Return the agent for ``role``, building it on first use.

        Raises:
            AgentNotRegistered: the role has no registration.
            AgentDisabled: the role is registered but disabled.
            AgentRegistrationError: the factory built an agent for another role.
        """
        registration = self._registration(role)
        if not registration.enabled:
            raise AgentDisabled(
                f"role '{role.value}' is disabled; enable it before dispatching work",
                role=role.value,
            )
        if registration.agent is None:
            factory = registration.factory
            if factory is None:  # pragma: no cover - register() always sets one
                raise AgentRegistrationError(f"role '{role.value}' has no factory")
            agent = factory()
            if not isinstance(agent, Agent):
                raise AgentRegistrationError(
                    f"factory for role '{role.value}' did not return an Agent"
                )
            if agent.role is not role:
                raise AgentRegistrationError(
                    f"factory for role '{role.value}' returned an agent for '{agent.role.value}'"
                )
            registration.agent = agent
            registration.built = True
        return registration.agent

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        """Check enablement and binding at call time, then delegate to the agent."""
        if not self.is_enabled(task.role):
            _ = self.resolve(task.role)  # raises the precise error (unknown vs disabled)
        # Fail fast before building a prompt if the role has no usable account. The
        # agent resolves again at the start of its own run, which is the authoritative
        # read; both happen at call time, so a hot-swapped binding is always seen.
        self.binding_for(task.role)
        return await self.resolve(task.role).run(task, run_state)
