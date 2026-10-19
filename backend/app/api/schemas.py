"""Response models for the API. Contracts stay in ``app.contracts``; these describe
the *surface* (health, config summaries) and never carry secrets."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.contracts.common import Contract, NonEmptyStr

__all__ = [
    "ConfigResponse",
    "ContractListResponse",
    "HealthResponse",
    "ProviderStatus",
    "RubricSummary",
    "ToolCatalogResponse",
    "ToolSpecResponse",
]


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
    """The rubric as the dashboard needs it: sections, gates and weights."""

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


class ToolSpecResponse(Contract):
    """One root tool as the console shows it: what it does, who may call it, what it needs."""

    name: NonEmptyStr
    summary: NonEmptyStr
    roles: list[str] = Field(description="Agent roles whose allowlist contains the tool.")
    token_cost_hint: int = Field(ge=0)
    requires: list[str] = Field(
        description="Dependencies the registry must be given before this tool is wired."
    )


class ToolCatalogResponse(Contract):
    """Root tool catalog plus the effective per-role allowlist. No secrets, no URLs."""

    tools: list[ToolSpecResponse]
    role_allowlist: dict[str, list[str]] = Field(
        description="role -> sorted tool names the role may see and call."
    )
