"""Response models for the API. Contracts stay in ``app.contracts``; these describe
the *surface* (health, config summaries) and never carry secrets.

The error models at the bottom are the **documentation** twin of what
:mod:`app.api.main` actually returns from its exception handlers
(``{"error": {code, message, context}}``). They are referenced from each route's
``responses=`` so the served ``/openapi.json`` declares the same status codes and
payloads as the frozen ``docs/openapi/openapi.yaml``. The handlers themselves keep
building plain dicts: an exception handler must never be able to fail on validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.contracts.common import Contract, NonEmptyStr

__all__ = [
    "ConfigResponse",
    "ContractListResponse",
    "ErrorBody",
    "ErrorEnvelope",
    "HealthResponse",
    "ProviderStatus",
    "RubricSummary",
    "ValidationErrorBody",
    "ValidationErrorContext",
    "ValidationErrorEnvelope",
    "ValidationFieldError",
]


class ProviderStatus(Contract):
    """Which providers are usable right now. No keys, no ciphertexts, no fingerprints."""

    mock: bool = True
    live_adapters_implemented: list[str] = Field(default_factory=list)
    accounts_configured: int = Field(default=0, ge=0)
    live_providers_enabled: bool = False
    account_store_ready: bool = Field(
        default=False,
        description="False when APP_MASTER_KEY is missing: the store is sealed.",
    )


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


# --------------------------------------------------------------------------- errors
class ErrorBody(Contract):
    """The ``error`` object every expected failure carries.

    Clients switch on ``code`` - never on ``message``, which is human prose and
    may be reworded at any time.
    """

    code: str = Field(
        min_length=1,
        max_length=64,
        description="Stable snake_case code; the closed set is documented per endpoint.",
    )
    message: str = Field(description="Human-readable, already redacted. Never parse this.")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Machine-readable detail: missing_fields/problems for needs_user_input, "
            "reason for conflict/run_conflict, errors for validation_error."
        ),
    )


class ErrorEnvelope(Contract):
    """The single error shape for every expected failure."""

    error: ErrorBody


class ValidationFieldError(Contract):
    """One rejected field. The submitted value is never included."""

    loc: str = Field(description="Dotted location, e.g. body.quote or query.limit.")
    msg: str
    type: str = Field(description="Pydantic error type, e.g. missing or greater_than_equal.")


class ValidationErrorContext(Contract):
    """What was wrong, field by field."""

    errors: list[ValidationFieldError] = Field(default_factory=list)
    missing_fields: list[str] | None = Field(
        default=None, description="Present when the failure is a missing field, not a bad one."
    )


class ValidationErrorBody(Contract):
    """The ``error`` object of a 422: the standard envelope with typed field errors."""

    code: Literal["validation_error"] = "validation_error"
    message: str
    context: ValidationErrorContext


class ValidationErrorEnvelope(Contract):
    """422 body - the standard envelope with typed field errors."""

    error: ValidationErrorBody
