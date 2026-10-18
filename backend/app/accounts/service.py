"""Account-store services: users, accounts, bindings, usage, audit, resolution.

One session, one owner, one mutation per method. The rules that make this safe:

* **Ownership is enforced per user** on every read and write. A request for another
  user's account raises :class:`NotFoundError` (404), never 403 - the API does not
  confirm that someone else's account id exists.
* **Keys are write-only.** A key enters through ``api_key`` and leaves as
  ``key_fingerprint`` + ``key_mask``. Plaintext exists only inside the encryptor
  and the provider it is handed to (registered for log redaction on the way).
* **Delete protection**: deleting the only account of an enabled role is refused
  (409) unless the caller names a replacement or explicitly disables the roles.
* **Audit everything**, with redacted detail only (HARD RULE 2).

The ``SqlAccountResolver`` / ``SqlUsageSink`` classes at the bottom implement the
provider-pool protocols: the pool resolves accounts at *call* time through them,
which is what makes hot-swap possible.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.auth import hash_password, verify_password
from app.accounts.crypto import EnvelopeEncryptor, mask_secret, redact_keys
from app.accounts.db import session_scope
from app.accounts.models import (
    AccountRow,
    AccountUsageDailyRow,
    AgentBindingRow,
    AuditLogRow,
    UserRow,
)
from app.accounts.schemas import (
    AccountCreate,
    AccountPatch,
    AccountTestResult,
    AccountView,
    BindingPut,
    BindingView,
    BudgetPatch,
    DeleteResult,
)
from app.contracts.account import Account, MonthlyBudget, key_fingerprint
from app.contracts.binding import AgentBinding
from app.contracts.enums import (
    AccountProvider,
    AccountStatus,
    BindingRole,
    BindingStrategy,
    MessageRole,
)
from app.contracts.llm import LLMMessage, LLMRequest
from app.core.errors import (
    AuthError,
    ConfigError,
    ConflictError,
    ContractViolation,
    NotFoundError,
    format_validation_errors,
)
from app.core.ids import new_ulid
from app.core.logging import get_logger
from app.core.pricing import estimate_cost
from app.providers.errors import NoAccountAvailable, ProviderError
from app.providers.pool import AccountResolver, UsageRecord, UsageSink  # noqa: F401

__all__ = [
    "AccountService",
    "AuditService",
    "BindingService",
    "ResolvedAccount",
    "SqlAccountResolver",
    "SqlUsageSink",
    "UserService",
    "validate_api_key",
]

logger = get_logger(__name__)

# --- provider key shapes ----------------------------------------------------
#: Claude *Console* API keys (console.anthropic.com). VERIFY against current docs.
_ANTHROPIC_API_KEY_RE = re.compile(r"^sk-ant-api[0-9A-Za-z_\-]{8,}$")
#: OAuth access tokens minted by a Claude Pro/Max subscription login. Never valid
#: credentials for the API - see the step note. VERIFY the prefix stays distinct.
_ANTHROPIC_OAUTH_TOKEN_RE = re.compile(r"^sk-ant-oat", re.IGNORECASE)

ANTHROPIC_KEY_HINT = (
    "Anthropic accounts need a Claude Console API key (console.anthropic.com -> API keys). "
    "A Claude Pro/Max subscription login is not an API credential and its session tokens "
    "are rejected here."
)


def validate_api_key(provider: AccountProvider, api_key: str) -> str:
    """Check a plaintext key against provider shape rules before encryption.

    Raises:
        ContractViolation: with an operator-actionable message (surfaced as 422).
    """
    key = api_key.strip()
    if not (8 <= len(key) <= 512):
        raise ContractViolation("api_key must be 8..512 characters")
    if provider is AccountProvider.ANTHROPIC:
        if _ANTHROPIC_OAUTH_TOKEN_RE.match(key):
            raise ContractViolation(
                "this looks like a Claude Pro/Max subscription token, not an API key. "
                + ANTHROPIC_KEY_HINT
            )
        if not _ANTHROPIC_API_KEY_RE.match(key):
            raise ContractViolation(
                f"api_key does not look like a Claude Console API key. {ANTHROPIC_KEY_HINT}"
            )
    return key


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _to_naive_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; normalise every row timestamp to aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------- audit
class AuditService:
    """Append-only audit trail. Detail is redacted JSON - never raw input."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        payload: str | None = None
        if detail:
            payload = redact_keys(json.dumps(detail, sort_keys=True, default=str))
        self._session.add(
            AuditLogRow(
                id=new_ulid(),
                actor=actor,
                action=action,
                target=target,
                ts=_utc_now(),
                detail=payload,
            )
        )


# --------------------------------------------------------------------------- users
class UserService:
    """Register and authenticate. Emails are stored lower-cased and unique."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self._session = session
        self._audit = audit if audit is not None else AuditService(session)

    def register(self, email: str, password: str) -> UserRow:
        normalised = email.strip().lower()
        existing = self._session.scalar(select(UserRow).where(UserRow.email == normalised))
        if existing is not None:
            raise ConflictError(f"an account with email {normalised!r} already exists")
        user = UserRow(
            id=new_ulid(),
            email=normalised,
            password_hash=hash_password(password),
            created_at=_utc_now(),
        )
        self._session.add(user)
        self._audit.record(actor=user.id, action="user.register", target=user.id)
        self._session.flush()
        return user

    def authenticate(self, email: str, password: str) -> UserRow:
        normalised = email.strip().lower()
        user = self._session.scalar(select(UserRow).where(UserRow.email == normalised))
        # Same error and (roughly) same work for unknown email and wrong password:
        # no user enumeration through the login form.
        if user is None:
            hash_password(password)  # burn comparable time
            raise AuthError("invalid email or password")
        if not verify_password(user.password_hash, password):
            raise AuthError("invalid email or password")
        self._audit.record(actor=user.id, action="user.login", target=user.id)
        return user


# --------------------------------------------------------------------------- accounts
@dataclass(frozen=True)
class ResolvedAccount:
    """An account plus its decrypted key, for exactly one provider call."""

    account: Account
    api_key: str


class AccountService:
    """CRUD + test for provider accounts. The only class allowed to decrypt keys."""

    def __init__(
        self,
        session: Session,
        encryptor: EnvelopeEncryptor | None,
        audit: AuditService | None = None,
    ) -> None:
        self._session = session
        self._encryptor = encryptor
        self._audit = audit if audit is not None else AuditService(session)

    def _require_encryptor(self) -> EnvelopeEncryptor:
        if self._encryptor is None:
            raise ConfigError(
                "APP_MASTER_KEY is not set; the encrypted account store is sealed. "
                "Set it (see .env.example) to add or use provider accounts."
            )
        return self._encryptor

    # -- reads ------------------------------------------------------------
    def list_for_owner(self, owner_id: str) -> list[AccountView]:
        rows = self._session.scalars(
            select(AccountRow)
            .where(AccountRow.owner_id == owner_id)
            .order_by(AccountRow.created_at, AccountRow.id)
        ).all()
        return [self.to_view(row) for row in rows]

    def get_row(self, owner_id: str, account_id: str) -> AccountRow:
        """The row, or 404 - including when it belongs to someone else."""
        row = self._session.get(AccountRow, account_id)
        if row is None or row.owner_id != owner_id:
            raise NotFoundError(f"account {account_id} not found")
        return row

    def get(self, owner_id: str, account_id: str) -> AccountView:
        return self.to_view(self.get_row(owner_id, account_id))

    def to_view(self, row: AccountRow) -> AccountView:
        return AccountView(
            id=row.id,
            owner_id=row.owner_id,
            provider=AccountProvider(row.provider),
            label=row.label,
            status=AccountStatus(row.status),
            base_url=row.base_url,
            key_fingerprint=key_fingerprint(row.key_ciphertext),
            key_mask=row.key_mask,
            budget=BudgetPatch(tokens=row.budget_tokens, usd=row.budget_usd),
            created_at=_to_naive_utc(row.created_at),
            updated_at=_to_naive_utc(row.updated_at),
        )

    def decrypt_key(self, row: AccountRow) -> str:
        """Plaintext key for one call. Registered for log redaction on the way out."""
        return self._require_encryptor().decrypt(row.key_ciphertext, aad=row.id)

    def resolve_for_call(self, account_id: str) -> ResolvedAccount:
        """Decrypt + contract-validate one account for the provider pool."""
        row = self._session.get(AccountRow, account_id)
        if row is None:
            raise NoAccountAvailable(f"account {account_id} no longer exists")
        if row.status == AccountStatus.DISABLED.value:
            raise NoAccountAvailable(f"account {account_id} is disabled")
        return ResolvedAccount(account=self.to_contract(row), api_key=self.decrypt_key(row))

    def to_contract(self, row: AccountRow) -> Account:
        """Build the frozen ``Account`` contract from a row (validates consistency)."""
        return Account(
            id=row.id,
            owner_id=row.owner_id,
            provider=AccountProvider(row.provider),
            label=row.label,
            key_ciphertext=row.key_ciphertext,
            base_url=row.base_url,
            status=AccountStatus(row.status),
            monthly_budget=MonthlyBudget(tokens=row.budget_tokens, usd=row.budget_usd),
        )

    # -- writes -----------------------------------------------------------
    def create(self, owner_id: str, payload: AccountCreate) -> AccountView:
        key = validate_api_key(payload.provider, payload.api_key.get_secret_value())
        account_id = new_ulid()
        row = AccountRow(
            id=account_id,
            owner_id=owner_id,
            provider=payload.provider.value,
            label=payload.label,
            key_ciphertext=self._require_encryptor().encrypt(key, aad=account_id),
            key_mask=mask_secret(key),
            base_url=payload.base_url,
            status=AccountStatus.ACTIVE.value,
            budget_tokens=payload.budget.tokens,
            budget_usd=payload.budget.usd,
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        self._session.add(row)
        self._audit.record(
            actor=owner_id,
            action="account.create",
            target=account_id,
            detail={
                "provider": payload.provider.value,
                "label": payload.label,
                "key_mask": row.key_mask,
            },
        )
        self._session.flush()
        return self.to_view(row)

    def patch(self, owner_id: str, account_id: str, payload: AccountPatch) -> AccountView:
        row = self.get_row(owner_id, account_id)
        changed: list[str] = []
        provider = AccountProvider(row.provider)

        if "api_key" in payload.model_fields_set and payload.api_key is not None:
            key = validate_api_key(provider, payload.api_key.get_secret_value())
            row.key_ciphertext = self._require_encryptor().encrypt(key, aad=row.id)
            row.key_mask = mask_secret(key)
            changed.append("api_key")
            self._audit.record(
                actor=owner_id,
                action="account.rotate_key",
                target=account_id,
                detail={"key_mask": row.key_mask},
            )

        if "label" in payload.model_fields_set and payload.label is not None:
            row.label = payload.label
            changed.append("label")

        if "base_url" in payload.model_fields_set:
            if payload.base_url is not None and provider is not AccountProvider.OPENAI_COMPATIBLE:
                raise ContractViolation(f"provider={provider.value} does not use base_url")
            if payload.base_url is None and provider is AccountProvider.OPENAI_COMPATIBLE:
                raise ContractViolation("provider=openai_compatible requires base_url")
            row.base_url = payload.base_url
            changed.append("base_url")

        if "budget" in payload.model_fields_set:
            budget = payload.budget or BudgetPatch()
            row.budget_tokens = budget.tokens
            row.budget_usd = budget.usd
            changed.append("budget")

        if "status" in payload.model_fields_set and payload.status is not None:
            row.status = payload.status.value
            changed.append("status")

        row.updated_at = _utc_now()
        # Contract-level consistency check on the whole row (provider/base_url rules).
        self.to_contract(row)
        if changed:
            self._audit.record(
                actor=owner_id,
                action="account.update",
                target=account_id,
                detail={"fields": changed, "status": row.status},
            )
        self._session.flush()
        return self.to_view(row)

    def delete(
        self,
        owner_id: str,
        account_id: str,
        *,
        replacement_account_id: str | None = None,
        disable_roles: bool = False,
    ) -> DeleteResult:
        """Delete an account without silently breaking enabled role bindings.

        The account that is the *only* candidate of an enabled role blocks the
        delete (409) unless the caller supplies ``replacement_account_id`` (swap
        first, then delete) or ``disable_roles=true`` (disable the affected roles,
        then delete). Multi-candidate bindings just lose the id; if that would leave
        a strategy that needs two accounts with one, the strategy relaxes to
        ``dedicated`` and says so in the audit trail.
        """
        row = self.get_row(owner_id, account_id)

        if replacement_account_id is not None:
            if replacement_account_id == account_id:
                raise ContractViolation(
                    "replacement_account_id must differ from the deleted account"
                )
            replacement = self.get_row(owner_id, replacement_account_id)
            if replacement.status == AccountStatus.DISABLED.value:
                raise ContractViolation("replacement account is disabled")
        else:
            replacement = None

        bindings = self._bindings_using(owner_id, account_id)
        sole_enabled = [b for b in bindings if b.enabled and list(b.account_ids) == [account_id]]
        if sole_enabled and replacement is None and not disable_roles:
            raise ConflictError(
                f"account {account_id} is the only account of enabled role(s) "
                f"{[b.role for b in sole_enabled]}; pass replacement_account_id or "
                "disable_roles=true",
                roles=[b.role for b in sole_enabled],
                account_id=account_id,
            )

        disabled_roles: list[str] = []
        replaced_in: list[str] = []
        removed_from: list[str] = []
        for binding in bindings:
            ids = list(binding.account_ids)
            if replacement is not None:
                ids = [replacement.id if item == account_id else item for item in ids]
                binding.account_ids = ids
                binding.updated_at = _utc_now()
                replaced_in.append(binding.role)
                continue
            if ids == [account_id]:
                # Sole candidate: keep the id recorded (bindings hold ids, not
                # rows) but disable the role so no call can select it. A binding
                # that was already disabled stays disabled and is not reported
                # as a fresh cascade.
                if binding.enabled:
                    binding.enabled = False
                    disabled_roles.append(binding.role)
                binding.updated_at = _utc_now()
                continue
            ids.remove(account_id)
            binding.account_ids = ids
            if len(ids) == 1 and binding.strategy in ("round_robin", "failover"):
                binding.strategy = "dedicated"
                self._audit.record(
                    actor=owner_id,
                    action="binding.strategy_relaxed",
                    target=f"role:{binding.role}",
                    detail={"strategy": "dedicated", "reason": "account_deleted"},
                )
            binding.updated_at = _utc_now()
            removed_from.append(binding.role)

        self._audit.record(
            actor=owner_id,
            action="account.delete",
            target=account_id,
            detail={
                "label": row.label,
                "provider": row.provider,
                "disabled_roles": disabled_roles,
                "replaced_in": replaced_in,
                "removed_from": removed_from,
            },
        )
        self._session.delete(row)
        self._session.flush()
        return DeleteResult(
            account_id=account_id,
            disabled_roles=disabled_roles,
            replaced_in=replaced_in,
            removed_from=removed_from,
        )

    async def test(self, owner_id: str, account_id: str) -> AccountTestResult:
        """One tiny live (or mock) call to prove the credential works. No key echoed."""
        from app.providers.factory import build_provider

        row = self.get_row(owner_id, account_id)
        provider_name = AccountProvider(row.provider)
        key = self.decrypt_key(row)
        provider = build_provider(self.to_contract(row), api_key=key)
        probe = LLMRequest(
            messages=[LLMMessage(role=MessageRole.USER, text="Reply with the single word: pong")],
            max_output_tokens=8,
        )
        started = time.monotonic()
        try:
            response = await provider.generate(probe, role="account_test", account_id=row.id)
            latency_ms = int((time.monotonic() - started) * 1000)
            await provider.aclose()
            self._audit.record(
                actor=owner_id,
                action="account.test",
                target=account_id,
                detail={"ok": True, "model": response.model, "latency_ms": latency_ms},
            )
            return AccountTestResult(
                ok=True,
                account_id=account_id,
                provider=provider_name,
                model=response.model,
                latency_ms=latency_ms,
            )
        except ProviderError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await provider.aclose()
            self._audit.record(
                actor=owner_id,
                action="account.test",
                target=account_id,
                detail={"ok": False, "error_code": exc.code, "latency_ms": latency_ms},
            )
            return AccountTestResult(
                ok=False,
                account_id=account_id,
                provider=provider_name,
                latency_ms=latency_ms,
                error_code=exc.code,
                error_message=redact_keys(exc.message),
            )

    def _bindings_using(self, owner_id: str, account_id: str) -> list[AgentBindingRow]:
        rows = self._session.scalars(
            select(AgentBindingRow).where(AgentBindingRow.user_id == owner_id)
        ).all()
        return [row for row in rows if account_id in list(row.account_ids)]


# --------------------------------------------------------------------------- bindings
class BindingService:
    """GET/PUT per-role bindings. Validated against the frozen ``AgentBinding`` contract."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self._session = session
        self._audit = audit if audit is not None else AuditService(session)

    def get(self, owner_id: str, role: BindingRole | str) -> BindingView | None:
        row = self._row(owner_id, role)
        return None if row is None else self.to_view(row)

    def put(self, owner_id: str, role: BindingRole | str, payload: BindingPut) -> BindingView:
        role_value = role.value if isinstance(role, BindingRole) else str(role)
        # Every candidate must be the caller's own account (no borrowed credentials).
        for account_id in payload.account_ids:
            account = self._session.get(AccountRow, account_id)
            if account is None or account.owner_id != owner_id:
                raise NotFoundError(f"account {account_id} not found")
        self._validate_role_provider(role_value, payload.account_ids)
        # The frozen contract is the validator: any rule it encodes is enforced here.
        try:
            AgentBinding(
                role=BindingRole(role_value),
                account_ids=list(payload.account_ids),
                strategy=payload.strategy,
                model=payload.model,
                max_input_tokens=payload.max_input_tokens,
                max_output_tokens=payload.max_output_tokens,
                enabled=payload.enabled,
            )
        except ValidationError as exc:
            raise ContractViolation(format_validation_errors(exc)) from exc

        row = self._row(owner_id, role_value)
        if row is None:
            row = AgentBindingRow(
                id=new_ulid(),
                user_id=owner_id,
                role=role_value,
                updated_at=_utc_now(),
            )
            self._session.add(row)
        row.account_ids = list(payload.account_ids)
        row.strategy = payload.strategy.value
        row.model = payload.model
        row.max_input_tokens = payload.max_input_tokens
        row.max_output_tokens = payload.max_output_tokens
        row.enabled = payload.enabled
        row.updated_at = _utc_now()
        self._audit.record(
            actor=owner_id,
            action="binding.put",
            target=f"role:{role_value}",
            detail={
                "account_ids": list(payload.account_ids),
                "strategy": payload.strategy.value,
                "model": payload.model,
                "enabled": payload.enabled,
            },
        )
        self._session.flush()
        return self.to_view(row)

    def to_view(self, row: AgentBindingRow) -> BindingView:
        return BindingView(
            role=BindingRole(row.role),
            account_ids=list(row.account_ids),
            strategy=BindingStrategy(row.strategy),
            model=row.model,
            max_input_tokens=row.max_input_tokens,
            max_output_tokens=row.max_output_tokens,
            enabled=row.enabled,
            updated_at=_to_naive_utc(row.updated_at) if row.updated_at else None,
        )

    def to_contract(self, row: AgentBindingRow) -> AgentBinding:
        return AgentBinding(
            role=BindingRole(row.role),
            account_ids=list(row.account_ids),
            strategy=BindingStrategy(row.strategy),
            model=row.model,
            max_input_tokens=row.max_input_tokens,
            max_output_tokens=row.max_output_tokens,
            enabled=row.enabled,
        )

    def _row(self, owner_id: str, role: BindingRole | str) -> AgentBindingRow | None:
        role_value = role.value if isinstance(role, BindingRole) else str(role)
        return self._session.scalar(
            select(AgentBindingRow).where(
                AgentBindingRow.user_id == owner_id, AgentBindingRow.role == role_value
            )
        )

    def _validate_role_provider(self, role: str, account_ids: list[str]) -> None:
        """The Planner plans through Claude: its accounts must be anthropic (or mock).

        Everything else may mix providers per binding. (Hard rule for the step: the
        planner's Anthropic key comes from the Claude Console, never a Pro/Max login -
        that shape check lives in :func:`validate_api_key`.)
        """
        if role != BindingRole.PLANNER.value:
            return
        for account_id in account_ids:
            account = self._session.get(AccountRow, account_id)
            if account is None or account.provider not in (
                AccountProvider.ANTHROPIC.value,
                AccountProvider.MOCK.value,
            ):
                raise ContractViolation(
                    "the planner role must bind anthropic (Claude Console key) or mock accounts"
                )


# --------------------------------------------------------------------------- usage
class SqlUsageSink:
    """Writes :class:`UsageRecord` roll-ups into ``account_usage_daily``.

    Takes a *session factory* (not a session): the pool outlives every request and
    emits after each call, so each record gets one short transaction.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def emit(self, record: UsageRecord) -> None:
        day: date = record.ts.astimezone(UTC).date() if record.ts.tzinfo else record.ts.date()
        with session_scope(self._factory) as session:
            row = session.get(
                AccountUsageDailyRow,
                (record.account_id, day, record.role, record.model),
            )
            if row is None:
                session.add(
                    AccountUsageDailyRow(
                        account_id=record.account_id,
                        date=day,
                        role=record.role,
                        model=record.model,
                        tokens_in=record.tokens_in,
                        tokens_out=record.tokens_out,
                        tokens_cached=record.tokens_cached,
                        est_cost=record.est_cost,
                    )
                )
            else:
                row.tokens_in += record.tokens_in
                row.tokens_out += record.tokens_out
                row.tokens_cached += record.tokens_cached
                row.est_cost += record.est_cost


# --------------------------------------------------------------------------- resolver
class SqlAccountResolver:
    """Pool-side account/binding resolution.

    Short-lived sessions per operation (the pool runs for the app's lifetime), and
    keys are opened only for the single call being resolved.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], encryptor: EnvelopeEncryptor | None
    ) -> None:
        self._factory = session_factory
        self._encryptor = encryptor

    def load_for_call(self, account_id: str) -> ResolvedAccount:
        with session_scope(self._factory) as session:
            service = AccountService(session, self._encryptor)
            resolved = service.resolve_for_call(account_id)
        # The key stays registered for redaction; the session is already closed.
        return resolved

    def get_binding(self, user_id: str, role: str) -> AgentBinding | None:
        with session_scope(self._factory) as session:
            bindings = BindingService(session)
            row = bindings._row(user_id, role)
            return None if row is None else bindings.to_contract(row)

    def set_account_status(
        self, account_id: str, status: AccountStatus, *, actor: str, reason: str
    ) -> None:
        with session_scope(self._factory) as session:
            row = session.get(AccountRow, account_id)
            if row is None:
                return
            row.status = status.value
            row.updated_at = _utc_now()
            AuditService(session).record(
                actor=actor,
                action="account.status_changed",
                target=account_id,
                detail={"status": status.value, "reason": reason},
            )


def usage_cost(model: str, tokens_in: int, tokens_out: int, tokens_cached: int) -> float:
    """est_cost for a usage record (see app.core.pricing; unknown models cost 0)."""
    from app.contracts.ledger import TokenUsage

    return estimate_cost(
        model,
        TokenUsage(**{"in": tokens_in, "out": tokens_out, "cached": tokens_cached}),
    )
