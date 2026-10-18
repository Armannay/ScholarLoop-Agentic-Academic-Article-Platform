"""The account store: provider keys encrypted at rest, resolved at call time.

HARD RULE 2 lives here. A key is accepted as plaintext only at the boundary
(:meth:`AccountStore.add` and :meth:`AccountStore.replace_key`); what the store
*keeps* is an AES-GCM ciphertext (see :mod:`app.accounts.crypto`). The plaintext
exists for the duration of one call and is never written to a log, a ledger entry
or an API response - :meth:`Account.public` is the only view that leaves the
process, and it carries a fingerprint instead of the key.

Accounts are resolved at call time (ADR-0004): the provider pool asks the store
for an account on every call, so disabling, rotating or deleting an account takes
effect immediately - including in the middle of a run.

The store is a :class:`Protocol` so persistence can change without touching a
single caller. The shipped implementation keeps ciphertexts in memory; a
SQLAlchemy-backed store can be added later without changing the pool, the
governor or the API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.accounts.crypto import SecretBox
from app.accounts.errors import AccountNotFound, AccountStoreError
from app.contracts.account import Account, MonthlyBudget
from app.contracts.common import NonEmptyStr
from app.contracts.enums import AccountProvider, AccountStatus
from app.core.clock import utc_now
from app.core.ids import new_ulid

__all__ = ["MOCK_KEY_PLACEHOLDER", "AccountStore", "InMemoryAccountStore"]

#: Stored instead of a key for ``provider=mock`` accounts, which never use one.
MOCK_KEY_PLACEHOLDER = "mock-provider-has-no-key"


@runtime_checkable
class AccountStore(Protocol):
    """What a store of provider accounts must offer.

    The pool depends on this protocol, not on a concrete store, so the same code
    serves the in-memory store used in development and a durable one later.
    """

    def add(
        self,
        *,
        owner_id: NonEmptyStr,
        provider: AccountProvider,
        label: NonEmptyStr,
        key: str | None = None,
        base_url: str | None = None,
        monthly_budget: MonthlyBudget | None = None,
    ) -> Account:
        """Create an account. ``key`` is plaintext and is encrypted before storage."""
        ...

    def get(self, account_id: str) -> Account | None:
        """Return the account, or ``None`` when it does not exist."""
        ...

    def require(self, account_id: str) -> Account:
        """Return the account or raise :class:`AccountNotFound`."""
        ...

    def list_accounts(
        self,
        *,
        owner_id: str | None = None,
        provider: AccountProvider | None = None,
        status: AccountStatus | None = None,
    ) -> list[Account]:
        """Every account matching the filters, oldest first."""
        ...

    def replace_key(self, account_id: str, key: str) -> Account:
        """Rotate the key of an account. The fingerprint changes."""
        ...

    def set_status(self, account_id: str, status: AccountStatus) -> Account:
        """Enable, disable or degrade an account."""
        ...

    def update_metadata(
        self,
        account_id: str,
        *,
        label: str | None = None,
        monthly_budget: MonthlyBudget | None = None,
    ) -> Account:
        """Change the metadata of an account (never the key)."""
        ...

    def delete(self, account_id: str) -> None:
        """Remove an account. Idempotent."""
        ...

    def reveal(self, account_id: str) -> str:
        """Decrypt and return the key. Only the store may do this."""
        ...

    def usage(self, account_id: str) -> int:
        """Tokens charged to the account in the current period."""
        ...

    def record_usage(self, account_id: str, tokens: int) -> None:
        """Charge ``tokens`` to the account for the current period."""
        ...

    def remaining(self, account: Account) -> int | None:
        """Tokens the account may still spend, or ``None`` when unlimited."""
        ...


class InMemoryAccountStore:
    """An account store that keeps ciphertexts in memory.

    Deliberately simple and deliberately *not* durable: it is the reference
    implementation of :class:`AccountStore`, used by the tests and by a dev
    server. The invariants it upholds are the ones a durable store must uphold
    too:

    * only ciphertexts are retained - a plaintext key is never stored, not even
      transiently, except as the argument of a call that is encrypting it;
    * the fingerprint of an account changes whenever the key is re-encrypted;
    * usage is counted per account and per period, so a monthly budget is
      meaningful.
    """

    def __init__(
        self,
        secret_box: SecretBox,
        *,
        id_factory: Callable[[], str] = new_ulid,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._box = secret_box
        self._id_factory = id_factory
        self._clock = clock
        self._accounts: dict[str, Account] = {}
        self._usage: dict[tuple[str, str], int] = {}

    # -- writes ------------------------------------------------------------
    def add(
        self,
        *,
        owner_id: NonEmptyStr,
        provider: AccountProvider,
        label: NonEmptyStr,
        key: str | None = None,
        base_url: str | None = None,
        monthly_budget: MonthlyBudget | None = None,
    ) -> Account:
        """Create an account and encrypt its key.

        Raises:
            AccountStoreError: a live provider was added without a key, or the
                label/owner is empty.
        """
        if not owner_id.strip():
            raise AccountStoreError("owner_id must not be empty")
        if not label.strip():
            raise AccountStoreError("label must not be empty")

        if key is None:
            if provider is not AccountProvider.MOCK:
                raise AccountStoreError(
                    f"provider '{provider.value}' needs a key; only 'mock' may be added without one"
                )
            secret = MOCK_KEY_PLACEHOLDER
        else:
            if not key.strip():
                raise AccountStoreError("the key must not be empty")
            secret = key

        account = Account(
            id=self._id_factory(),
            owner_id=owner_id,
            provider=provider,
            label=label,
            key_ciphertext=self._box.seal(secret),
            base_url=base_url,
            status=AccountStatus.ACTIVE,
            monthly_budget=monthly_budget or MonthlyBudget(),
        )
        self._accounts[account.id] = account
        return account

    def replace_key(self, account_id: str, key: str) -> Account:
        """Rotate the key of an account.

        The stored ciphertext is replaced by a freshly sealed one, so the account's
        fingerprint changes: the UI can prove the key was updated without ever
        seeing it.
        """
        if not key.strip():
            raise AccountStoreError("the key must not be empty")
        account = self.require(account_id)
        updated = account.model_copy(update={"key_ciphertext": self._box.seal(key)})
        self._accounts[account_id] = updated
        return updated

    def set_status(self, account_id: str, status: AccountStatus) -> Account:
        """Change an account's status (enable, disable, degrade)."""
        account = self.require(account_id)
        updated = account.model_copy(update={"status": status})
        self._accounts[account_id] = updated
        return updated

    def update_metadata(
        self,
        account_id: str,
        *,
        label: str | None = None,
        monthly_budget: MonthlyBudget | None = None,
    ) -> Account:
        """Change the metadata of an account.

        The key is not reachable from here on purpose: rotating a key is a
        separate, auditable operation.
        """
        account = self.require(account_id)
        changes: dict[str, object] = {}
        if label is not None:
            if not label.strip():
                raise AccountStoreError("label must not be empty")
            changes["label"] = label
        if monthly_budget is not None:
            changes["monthly_budget"] = monthly_budget
        if not changes:
            return account
        updated = account.model_copy(update=changes)
        self._accounts[account_id] = updated
        return updated

    def delete(self, account_id: str) -> None:
        """Remove an account. Deleting an unknown account is not an error."""
        self._accounts.pop(account_id, None)
        for key in [key for key in self._usage if key[0] == account_id]:
            del self._usage[key]

    # -- reads -------------------------------------------------------------
    def get(self, account_id: str) -> Account | None:
        """Return the account, or ``None`` when it does not exist."""
        return self._accounts.get(account_id)

    def require(self, account_id: str) -> Account:
        """Return the account or raise :class:`AccountNotFound`."""
        account = self._accounts.get(account_id)
        if account is None:
            raise AccountNotFound(f"no account with id {account_id!r}")
        return account

    def list_accounts(
        self,
        *,
        owner_id: str | None = None,
        provider: AccountProvider | None = None,
        status: AccountStatus | None = None,
    ) -> list[Account]:
        """Every account matching the filters, in insertion order."""
        return [
            account
            for account in self._accounts.values()
            if (owner_id is None or account.owner_id == owner_id)
            and (provider is None or account.provider is provider)
            and (status is None or account.status is status)
        ]

    def reveal(self, account_id: str) -> str:
        """Decrypt the key of an account.

        This is the only method in the whole application that returns a plaintext
        key. Callers must not log it, store it or put it in a prompt.
        """
        account = self.require(account_id)
        return self._box.open(account.key_ciphertext)

    # -- usage -------------------------------------------------------------
    def usage(self, account_id: str) -> int:
        """Tokens charged to the account in the current period."""
        return self._usage.get((account_id, self._period()), 0)

    def record_usage(self, account_id: str, tokens: int) -> None:
        """Charge ``tokens`` to the account for the current period."""
        if tokens <= 0:
            return
        key = (account_id, self._period())
        self._usage[key] = self._usage.get(key, 0) + tokens

    def remaining(self, account: Account) -> int | None:
        """Tokens the account may still spend this period, or ``None`` if unlimited."""
        cap = account.monthly_budget.tokens
        if cap is None:
            return None
        return max(0, cap - self.usage(account.id))

    # -- internals ---------------------------------------------------------
    def _period(self) -> str:
        """The billing period key (``YYYY-MM``) of the current month."""
        moment: datetime = self._clock()
        return f"{moment.year:04d}-{moment.month:02d}"
