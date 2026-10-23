"""LLMRequest / LLMResponse - the provider boundary.

Deliberately the narrowest possible surface: a request is system blocks plus
messages plus a cap plus an optional JSON schema; a response is text plus usage.

``cache`` is a *request* hint ("this block is stable, cache it"), consumed by
providers that support prompt caching; it is not a promise about the response.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, model_validator

from app.contracts.common import Contract, NonEmptyStr, VersionedContract
from app.contracts.enums import MessageRole
from app.contracts.ledger import TokenUsage

__all__ = ["LLMMessage", "LLMRequest", "LLMResponse", "SystemBlock"]


class SystemBlock(Contract):
    """A system prompt block. Split into blocks so the stable prefix can be cached."""

    text: NonEmptyStr = Field(max_length=200_000)
    cache: bool = Field(default=False, description="Hint: keep this block in the prompt cache.")


class LLMMessage(Contract):
    """One conversational turn. System text never appears here."""

    role: MessageRole
    text: NonEmptyStr = Field(max_length=500_000)


class LLMRequest(VersionedContract):
    """One provider call. Never carries an API key - the provider is already bound."""

    system_blocks: list[SystemBlock] = Field(default_factory=list, max_length=16)
    messages: list[LLMMessage] = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(ge=1, le=64_000)
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description="When set, the provider must return JSON conforming to this schema.",
    )

    @model_validator(mode="after")
    def _schema_shape(self) -> LLMRequest:
        if self.json_schema is not None:
            if not isinstance(self.json_schema, dict) or not self.json_schema:
                raise ValueError("json_schema must be a non-empty object")
            if "type" not in self.json_schema and "$ref" not in self.json_schema:
                raise ValueError("json_schema needs a 'type' or '$ref' key")
        roles = [message.role for message in self.messages]
        if roles[-1] is not MessageRole.USER:
            raise ValueError("the last message must come from the user; the model answers it")
        return self

    @property
    def is_structured(self) -> bool:
        """True when the caller requires JSON output (HARD RULE 3: schema over prose)."""
        return self.json_schema is not None


class LLMResponse(VersionedContract):
    """One provider answer, with usage so the governor can bill it."""

    # An empty completion is a *model* outcome (content filtering, a stop at
    # max_tokens), not a transport failure: providers must be able to report it so
    # the agent runtime treats it as an invalid answer and earns one repair. The
    # inherited ``str_min_length`` would instead raise ValidationError inside the
    # adapter, where nothing catches it. ``model`` keeps its explicit min_length.
    model_config = ConfigDict(str_min_length=0)

    text: str = Field(description="Raw model text; structured replies are still JSON *text*.")
    usage: TokenUsage
    model: NonEmptyStr = Field(max_length=120, description="Model the provider actually used.")
    latency_ms: int = Field(ge=0, le=3_600_000)

    @property
    def total_tokens(self) -> int:
        """Tokens billed for this response."""
        return self.usage.total
