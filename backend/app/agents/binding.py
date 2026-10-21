"""Binding resolvers: the call-time seam between an agent and an account binding.

``AgentBinding`` names *account ids*, never keys, and the user may add, disable or
delete an account between two calls. An agent therefore resolves its binding at the
start of every :meth:`~app.agents.base.Agent.run`, through one of these resolvers,
and never stores the result.

The pool and the account store already expose ``get_binding(user_id, role)``;
:class:`UserBindingResolver` adapts that method to the role-only protocol the agent
runtime expects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from app.contracts.binding import AgentBinding
from app.contracts.enums import BindingRole

__all__ = [
    "BindingStore",
    "CallableBindingResolver",
    "MappingBindingResolver",
    "UserBindingResolver",
]


@runtime_checkable
class BindingStore(Protocol):
    """The slice of the account store the runtime needs (no keys, no SQL)."""

    def get_binding(self, user_id: str, role: str) -> AgentBinding | None:
        """The user's binding for ``role``, or ``None`` when they have none."""
        ...


class CallableBindingResolver:
    """Resolve through a function, read on every call.

    The canonical hot-swap test double: the test mutates a variable the closure
    reads, and the next call sees the new binding without rebuilding the agent.
    """

    def __init__(self, resolver: Callable[[BindingRole], AgentBinding | None]) -> None:
        self._resolver = resolver

    def resolve(self, role: BindingRole) -> AgentBinding | None:
        """Delegate to the wrapped callable."""
        return self._resolver(role)


class MappingBindingResolver:
    """Resolve from a mapping. Handy for tests and for a single-user local run."""

    def __init__(self, bindings: Mapping[BindingRole, AgentBinding]) -> None:
        self._bindings = dict(bindings)

    def resolve(self, role: BindingRole) -> AgentBinding | None:
        """Return the mapped binding for ``role``."""
        return self._bindings.get(role)


class UserBindingResolver:
    """Resolve one user's binding through the account store, at every call."""

    def __init__(self, store: BindingStore, *, user_id: str) -> None:
        if not user_id:
            raise ValueError("user_id must not be empty")
        self._store = store
        self._user_id = user_id

    @property
    def user_id(self) -> str:
        """Owner whose bindings this resolver reads."""
        return self._user_id

    def resolve(self, role: BindingRole) -> AgentBinding | None:
        """Read the binding from the store now - never from a cached copy."""
        return self._store.get_binding(self._user_id, role.value)
