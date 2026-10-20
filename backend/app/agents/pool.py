"""Adapt :class:`~app.providers.pool.ProviderPool` to the agent provider protocol.

The pool is the established way to reach a real account: it resolves the binding,
picks a candidate, decrypts the key, applies failover and bills usage. Its
``generate`` also needs the owner's ``user_id``, which the runtime does not carry, so
the orchestrator wraps one pool per user with this thin adapter. No secrets, no new
routing: the adapter only supplies the missing argument.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.contracts.llm import LLMRequest, LLMResponse

__all__ = ["PoolAgentProvider", "PoolLike"]


@runtime_checkable
class PoolLike(Protocol):
    """The slice of ``ProviderPool`` the adapter calls."""

    async def generate(
        self,
        request: LLMRequest,
        *,
        role: str,
        user_id: str,
        model: str | None = None,
    ) -> LLMResponse:
        """One pooled call for a user and role."""
        ...


class PoolAgentProvider:
    """Bind a pool to one user, and expose the agent-facing ``generate``."""

    name = "pool"

    def __init__(self, pool: PoolLike, *, user_id: str) -> None:
        if not user_id:
            raise ValueError("user_id must not be empty")
        self._pool = pool
        self._user_id = user_id

    @property
    def user_id(self) -> str:
        """Owner whose accounts the pool resolves."""
        return self._user_id

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
    ) -> LLMResponse:
        """Delegate to the pool, which resolves the account at this moment."""
        if role is None:
            raise ValueError("PoolAgentProvider needs the binding role for every call")
        return await self._pool.generate(request, role=role, user_id=self._user_id, model=model)
