"""The provider boundary: one Protocol, one error family, one token convention.

Every adapter (live vendors later, :class:`~app.providers.mock.MockProvider` now)
implements :class:`LLMProvider` and nothing else. The orchestrator and the governor
never see an SDK object, so HARD RULES 3 and 5 are enforceable by construction: the
only way to spend tokens is through ``generate()``, and the only implementation in
tests is the mock.

Token convention (shared by adapters and enforced by MockProvider):

* ``LLMRequest`` is measured in tokens as ``ceil(chars / 4)`` plus a fixed per-message
  overhead - the documented approximation, good enough for budgeting;
* a response reports non-overlapping usage (see
  :class:`~app.contracts.ledger.TokenUsage`): full-rate prompt, cached prompt, output.
"""

from __future__ import annotations

import math
from typing import ClassVar, Protocol, runtime_checkable

from app.contracts.binding import AgentBinding
from app.contracts.llm import LLMRequest, LLMResponse

__all__ = [
    "CHARS_PER_TOKEN",
    "MESSAGE_OVERHEAD_TOKENS",
    "LLMProvider",
    "estimate_tokens",
    "prompt_tokens",
]

#: Documented approximation used everywhere in ScholarLoop: 4 characters per token.
CHARS_PER_TOKEN: int = 4
#: Role markers, delimiters and similar per-message overhead.
MESSAGE_OVERHEAD_TOKENS: int = 4


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate for a string: ``ceil(len(text) / 4)``.

    Empty text is 0 tokens; any non-empty text is at least 1 token. Never call a
    vendor tokeniser for budgeting - it costs a network round trip and buys nothing
    at this precision.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def prompt_tokens(request: LLMRequest) -> int:
    """Token estimate for a whole request: system blocks + messages + overhead."""
    system = sum(estimate_tokens(block.text) for block in request.system_blocks)
    messages = sum(
        estimate_tokens(message.text) + MESSAGE_OVERHEAD_TOKENS for message in request.messages
    )
    return system + messages


def cached_prompt_tokens(request: LLMRequest) -> int:
    """Tokens of the request that a cache-aware provider could serve from cache."""
    return sum(estimate_tokens(block.text) for block in request.system_blocks if block.cache)


@runtime_checkable
class LLMProvider(Protocol):
    """What an adapter must implement. Async-only: every call is I/O."""

    #: Stable identifier used in logs and the ledger, e.g. ``mock`` or ``anthropic``.
    name: ClassVar[str]

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        """Execute one call.

        Args:
            request: the prompt, its cap and an optional JSON schema.
            model: model id resolved from the role's binding; ``None`` = provider default.
            role: the binding role making the call; adapters may ignore it, the mock
                uses it to select scripted replies.
            account_id: which account to bill. Never a key: the provider already holds
                the credential it was constructed with.

        Raises:
            ProviderError: any of the subclasses in this package. Nothing else.
        """
        ...

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Best-effort prompt size in tokens, or ``None`` when unknown.

        ``None`` is a legitimate answer (a vendor may not expose a tokeniser); the
        governor then falls back to :func:`prompt_tokens`.
        """
        ...

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
        ...


class ProviderRouting(Protocol):
    """Optional capability: report which binding/model a call would use.

    Live adapters implement :meth:`LLMProvider` only; the routing helper exists so the
    orchestrator can ask "what would you use for this role" before spending anything.
    """

    def resolve_binding(self, role: str) -> AgentBinding: ...


__all__.append("ProviderRouting")
