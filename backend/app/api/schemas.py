"""Request and response models for the API.

Contracts stay in ``app.contracts``; these describe the *surface* (health, config
summaries, account administration) and never carry secrets. A request may carry a
provider key - that is the only way a key ever enters the process - so key
fields are :class:`pydantic.SecretStr` and are never echoed back.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr, field_validator

from app.contracts.account import MonthlyBudget
from app.contracts.common import Contract, NonEmptyStr
from app.contracts.enums import AccountProvider, AccountStatus

__all__ = [
    "AccountCreateRequest",
    "AccountKeyRequest",
    "AccountUpdateRequest",
    "ConfigResponse",
    "ContractListResponse",
    "HealthResponse",
    "ProviderStatus",
    "RubricSummary",
]


class AccountCreateRequest(Contract):
    """Add a provider account.

    ``key`` is the only place a plaintext provider key is accepted. The account
    store encrypts it before it is retained, and no endpoint ever returns it.
    """

    owner_id: NonEmptyStr = Field(default="default", max_length=64)
    provider: AccountProvider
    label: NonEmptyStr = Field(max_length=64)
    key: SecretStr | None = Field(
        default=None,
        description="Provider API key. Only 'mock' accounts may be added without one.",
    )
    base_url: str | None = Field(default=None, max_length=500)
    monthly_budget: MonthlyBudget | None = None

    @field_validator("key")
    @classmethod
    def _clean_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Trim a pasted key and refuse an empty one."""
        if value is None:
            return None
        cleaned = value.get_secret_value().strip()
        if not cleaned:
            raise ValueError("the key must not be empty")
        return SecretStr(cleaned)


class AccountUpdateRequest(Contract):
    """Change the metadata of an account. The key is rotated through its own route."""

    label: NonEmptyStr | None = Field(default=None, max_length=64)
    status: AccountStatus | None = None
    monthly_budget: MonthlyBudget | None = None


class AccountKeyRequest(Contract):
    """Rotate the key of an account."""

    key: SecretStr

    @field_validator("key")
    @classmethod
    def _clean_key(cls, value: SecretStr) -> SecretStr:
        """Trim a pasted key and refuse an empty one."""
        cleaned = value.get_secret_value().strip()
        if not cleaned:
            raise ValueError("the key must not be empty")
        return SecretStr(cleaned)


class ProviderStatus(Contract):
    """Which providers are usable right now. No keys, no ciphertexts, no fingerprints."""

    mock: bool = True
    live_adapters_implemented: list[str] = Field(default_factory=list)
    accounts_configured: int = Field(default=0, ge=0)
    live_providers_enabled: bool = False


class HealthResponse(Contract):
    """Liveness plus the two facts that decide whether a run can start."""

    status: str = Field(description="ok | degraded")
    version: NonEmptyStr
    contract_version: int
    env: NonEmptyStr
    policies_loaded: bool
    rubric_loaded: bool
    providers: ProviderStatus
    config_errors: dict[str, str] = Field(default_factory=dict)


class RubricSummary(Contract):
    """The rubric as the dashboard needs it: sections, gates, weights."""

    article_types: dict[str, list[str]] = Field(description="Article type -> required section ids.")
    gates: list[str] = Field(description="The 8 hard gate names, in canonical order.")
    criteria_weights: dict[str, float] = Field(description="Soft criteria weights.")
    weights_total: float
    abstract_word_range: dict[str, int] = Field(description="min/max for the default article type.")


class ConfigResponse(Contract):
    """Effective configuration: policies after every override has been merged."""

    contract_version: int
    policies_path: str
    overrides_path: str | None = None
    project: str | None = None
    policies: dict[str, Any] = Field(description="Deep-merged, validated policy values.")
    rubric: RubricSummary


class ContractListResponse(Contract):
    """The contract registry, so clients can discover schema endpoints."""

    contract_version: int
    names: list[str]
    schema_url_template: str
