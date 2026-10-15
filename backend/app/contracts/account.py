"""Account - a provider credential, encrypted at rest and resolved at call time.

HARD RULE 2. ``key_ciphertext`` is opaque to everything above the account store: it
is produced by the encryptor, never logged, never put in a prompt, and never
rendered by the API (see ``AccountPublic``). There is no plaintext key field, so a
plaintext key cannot enter a contract by accident.
"""

from __future__ import annotations

import hashlib

from pydantic import Field, field_validator, model_validator

from app.contracts.common import Contract, NonEmptyStr, Ulid, VersionedContract
from app.contracts.enums import AccountProvider, AccountStatus

__all__ = ["KEY_FINGERPRINT_LENGTH", "Account", "AccountPublic", "MonthlyBudget", "key_fingerprint"]

KEY_FINGERPRINT_LENGTH = 12


def key_fingerprint(ciphertext: str) -> str:
    """Short stable fingerprint of a ciphertext blob.

    Fingerprinting the *ciphertext* (not the key) means the fingerprint can be shown
    in the UI - "account updated" - without ever touching key material. Encryption is
    randomised, so this changes on every key update, which is exactly the point.
    """
    digest = hashlib.sha256(ciphertext.encode("utf-8")).hexdigest()[:KEY_FINGERPRINT_LENGTH]
    return digest.upper()


class MonthlyBudget(Contract):
    """Optional spend ceiling per account. ``None`` means "inherit the run budget"."""

    tokens: int | None = Field(default=None, ge=1_000, le=1_000_000_000)
    usd: float | None = Field(default=None, gt=0.0, le=1_000_000.0)

    @property
    def is_unlimited(self) -> bool:
        """True when neither ceiling is set."""
        return self.tokens is None and self.usd is None


class Account(VersionedContract):
    """A provider account owned by a user. Users add/remove/disable these at any time."""

    id: Ulid
    owner_id: NonEmptyStr = Field(max_length=64)
    provider: AccountProvider
    label: NonEmptyStr = Field(max_length=64, description="Human label, e.g. 'work-anthropic'.")
    key_ciphertext: str = Field(
        min_length=1,
        max_length=8192,
        repr=False,
        description="Encrypted key blob (base64 AES-GCM). Opaque; never decrypted above the store.",
    )
    base_url: str | None = Field(
        default=None,
        max_length=500,
        description="Only meaningful for provider=openai_compatible.",
    )
    status: AccountStatus = AccountStatus.ACTIVE
    monthly_budget: MonthlyBudget = Field(default_factory=MonthlyBudget)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an absolute http(s) URL: {value!r}")
        return value

    @field_validator("key_ciphertext")
    @classmethod
    def _no_looks_like_plaintext_key(cls, value: str) -> str:
        """Reject the obvious mistake of storing a raw provider key.

        A defence-in-depth check, not a substitute for the encryptor: it catches the
        copy-paste error where someone skips the account store entirely.
        """
        upper = value.upper()
        for prefix in ("SK-ANT", "SK-ANTHROPIC", "SK-PROJ", "SK-OPENAI", "AKIA"):
            if upper.startswith(prefix):
                raise ValueError(
                    "key_ciphertext looks like a raw API key; encrypt it via the account store"
                )
        return value

    @model_validator(mode="after")
    def _provider_consistency(self) -> Account:
        if self.provider is AccountProvider.MOCK and self.base_url is not None:
            raise ValueError("provider=mock does not use base_url")
        if self.provider is AccountProvider.ANTHROPIC and self.base_url is not None:
            raise ValueError("provider=anthropic uses the vendor endpoint; leave base_url empty")
        return self

    @property
    def is_usable(self) -> bool:
        """True when the account may be selected for a call right now."""
        return self.status is not AccountStatus.DISABLED

    def public(self) -> AccountPublic:
        """Redacted view safe to log, return from the API, or put in a digest."""
        return AccountPublic(
            id=self.id,
            owner_id=self.owner_id,
            provider=self.provider,
            label=self.label,
            status=self.status,
            base_url=self.base_url,
            key_fingerprint=key_fingerprint(self.key_ciphertext),
            monthly_budget=self.monthly_budget,
        )


class AccountPublic(Contract):
    """An account with the key replaced by a short, non-reversible fingerprint."""

    id: Ulid
    owner_id: NonEmptyStr
    provider: AccountProvider
    label: NonEmptyStr
    status: AccountStatus
    base_url: str | None = None
    key_fingerprint: str = Field(
        min_length=KEY_FINGERPRINT_LENGTH,
        max_length=KEY_FINGERPRINT_LENGTH,
        description="First 12 hex chars of sha256(ciphertext); proves 'the key changed'.",
    )
    monthly_budget: MonthlyBudget = Field(default_factory=MonthlyBudget)
