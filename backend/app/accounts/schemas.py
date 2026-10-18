"""Request/response models for the accounts API surface.

These describe the *surface* - they are not contracts (:mod:`app.contracts` stays
the wire source of truth for run data). The one hard rule they encode structurally:
no model here has a field that can *return* key material. ``api_key`` appears only
on request models, as ``SecretStr``, so a repr, traceback or log line shows
``**********`` instead of the key (HARD RULE 2).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, SecretStr, field_validator, model_validator

from app.accounts.auth import EMAIL_PATTERN, MIN_PASSWORD_LENGTH, is_valid_email
from app.contracts.common import Contract, NonEmptyStr, Ulid
from app.contracts.enums import AccountProvider, AccountStatus, BindingRole, BindingStrategy
from app.contracts.llm import LLMRequest  # noqa: F401  (kept importable for callers)

__all__ = [
    "AccountCreate",
    "AccountPatch",
    "AccountTestResult",
    "AccountView",
    "BindingPut",
    "BindingView",
    "BudgetPatch",
    "DeleteResult",
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    "UsageDailyView",
    "UserView",
]

EMAIL_FIELD = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)


class BudgetPatch(Contract):
    """Monthly spend ceiling, mirroring ``MonthlyBudget``. Both ``None`` = unlimited."""

    tokens: int | None = Field(default=None, ge=1_000, le=1_000_000_000)
    usd: float | None = Field(default=None, gt=0.0, le=1_000_000.0)


class AccountCreate(Contract):
    """POST /accounts. ``api_key`` is write-only: it is accepted and never returned."""

    provider: AccountProvider
    label: NonEmptyStr = Field(max_length=64)
    api_key: SecretStr = Field(min_length=1, max_length=512)
    base_url: str | None = Field(default=None, max_length=500)
    budget: BudgetPatch = Field(default_factory=BudgetPatch)

    @field_validator("base_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def _provider_rules(self) -> AccountCreate:
        if self.provider is AccountProvider.OPENAI_COMPATIBLE and self.base_url is None:
            raise ValueError("provider=openai_compatible requires base_url")
        if self.provider is not AccountProvider.OPENAI_COMPATIBLE and self.base_url is not None:
            raise ValueError(f"provider={self.provider.value} does not use base_url")
        return self


class AccountPatch(Contract):
    """PATCH /accounts/{id}: rotate key, relabel, move base_url, reset budget/status.

    ``model_fields_set`` distinguishes "not sent" from "send null" for budget,
    base_url and status, so an explicit null clears the budget / base_url.
    """

    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    label: NonEmptyStr | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=500)
    budget: BudgetPatch | None = None
    status: AccountStatus | None = None

    @field_validator("base_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be an absolute http(s) URL")
        return value


class AccountView(Contract):
    """What the API returns for an account: fingerprint + mask, never the key."""

    id: Ulid
    owner_id: Ulid
    provider: AccountProvider
    label: NonEmptyStr
    status: AccountStatus
    base_url: str | None = None
    key_fingerprint: str = Field(min_length=12, max_length=12)
    key_mask: str = Field(description="``****`` + last 4 chars of the key, set at write time.")
    budget: BudgetPatch = Field(default_factory=BudgetPatch)
    created_at: datetime
    updated_at: datetime


class DeleteResult(Contract):
    """DELETE outcome. ``disabled_roles``/``replaced_in`` explain the cascade."""

    deleted: bool = True
    account_id: Ulid
    disabled_roles: list[str] = Field(default_factory=list)
    replaced_in: list[str] = Field(default_factory=list)
    removed_from: list[str] = Field(default_factory=list)


class AccountTestResult(Contract):
    """POST /accounts/{id}/test: reachability + auth, with no key ever echoed."""

    ok: bool
    account_id: Ulid
    provider: AccountProvider
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None


class BindingPut(Contract):
    """PUT /agents/{role}/binding. Validated against the ``AgentBinding`` contract."""

    account_ids: list[Ulid] = Field(min_length=1, max_length=16)
    strategy: BindingStrategy = BindingStrategy.FAILOVER
    model: NonEmptyStr = Field(max_length=120)
    max_input_tokens: int = Field(ge=1_000, le=2_000_000)
    max_output_tokens: int = Field(ge=64, le=64_000)
    enabled: bool = True


class BindingView(Contract):
    """GET /agents/{role}/binding. Ids only - accounts resolve at call time."""

    role: BindingRole
    account_ids: list[Ulid]
    strategy: BindingStrategy
    model: str
    max_input_tokens: int
    max_output_tokens: int
    enabled: bool
    updated_at: datetime | None = None


class UsageDailyView(Contract):
    """One (account, day, role, model) usage roll-up."""

    account_id: Ulid
    date: date
    role: str
    model: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tokens_cached: int = Field(ge=0)
    est_cost: float = Field(ge=0.0)


class RegisterRequest(Contract):
    """POST /auth/register."""

    email: str = EMAIL_FIELD
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)

    @field_validator("email")
    @classmethod
    def _known_email_shape(cls, value: str) -> str:
        if not is_valid_email(value):
            raise ValueError(f"not a valid email address: {value!r}")
        return value.strip().lower()


class LoginRequest(Contract):
    """POST /auth/login."""

    email: str = EMAIL_FIELD
    password: str = Field(min_length=1, max_length=256)


class UserView(Contract):
    """The user as the API exposes it. ``password_hash`` has no field here at all."""

    id: Ulid
    email: str
    created_at: datetime


class TokenResponse(Contract):
    """Bearer token plus the user it belongs to."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 (wire field name, not a password)
    user: UserView
