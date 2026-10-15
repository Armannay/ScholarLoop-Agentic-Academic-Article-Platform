"""AgentBinding - which accounts and model a role uses.

Bindings hold *account ids*, never accounts or keys: accounts are resolved at call
time (users may delete an account mid-run), and a binding that pointed at a deleted
account would silently keep using a credential that no longer exists.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.contracts.common import NonEmptyStr, Ulid, VersionedContract
from app.contracts.enums import BindingRole, BindingStrategy

__all__ = ["AgentBinding"]

#: Token ceilings must leave room for an answer; the config loader checks the same bound.
MIN_OUTPUT_TOKENS = 64
MAX_CONTEXT_TOKENS = 2_000_000


class AgentBinding(VersionedContract):
    """Per-role provider configuration."""

    role: BindingRole
    account_ids: list[Ulid] = Field(
        min_length=1,
        max_length=16,
        description="Ordered candidate accounts, resolved at call time.",
    )
    strategy: BindingStrategy = BindingStrategy.FAILOVER
    model: NonEmptyStr = Field(max_length=120, description="Provider-specific model id.")
    max_input_tokens: int = Field(ge=1_000, le=MAX_CONTEXT_TOKENS)
    max_output_tokens: int = Field(ge=MIN_OUTPUT_TOKENS, le=64_000)
    enabled: bool = True

    @model_validator(mode="after")
    def _strategy_needs_candidates(self) -> AgentBinding:
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("account_ids must be unique; a repeated account is not a fallback")
        if self.strategy in {BindingStrategy.ROUND_ROBIN, BindingStrategy.FAILOVER} and (
            len(self.account_ids) < 2
        ):
            raise ValueError(
                f"strategy '{self.strategy.value}' needs at least two accounts; "
                "use dedicated for a single-account binding"
            )
        if self.max_output_tokens > self.max_input_tokens:
            raise ValueError("max_output_tokens must not exceed max_input_tokens")
        return self

    @property
    def is_active(self) -> bool:
        """True when this binding may be used for a call."""
        return self.enabled and bool(self.account_ids)
