"""The account store and its crypto: keys are encrypted at rest, never stored.

HARD RULE 2 in one module. The properties under test are the ones the rest of the
application relies on:

* what the store keeps is a ciphertext, never a key;
* the key comes back only through :meth:`AccountStore.reveal`;
* a fingerprint proves *that* a key exists, never *which* key it is;
* tampering and wrong secrets are refused loudly;
* disabling an account takes effect immediately (ADR-0004).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.accounts.crypto import CIPHER_VERSION, SecretBox, SecretBoxError
from app.accounts.errors import AccountNotFound, AccountStoreError
from app.accounts.store import InMemoryAccountStore
from app.contracts.account import Account, MonthlyBudget
from app.contracts.enums import AccountProvider, AccountStatus
from app.core.ids import new_ulid
from app.core.redaction import find_secrets

#: A synthetic key, built by concatenation so no key-shaped string is ever in the
#: repository (HARD RULE 2).
FAKE_KEY = "unit-" + "test-" + "key-" + "0123456789abcdef"
OTHER_FAKE_KEY = "unit-" + "test-" + "key-" + "fedcba9876543210"
STORE_SECRET = "unit-test-store-secret-not-a-real-secret"


@pytest.fixture
def box() -> SecretBox:
    """A secret box derived from a synthetic secret."""
    return SecretBox(STORE_SECRET)


@pytest.fixture
def store(box: SecretBox) -> InMemoryAccountStore:
    """An in-memory store with deterministic ids and a fixed clock."""
    counter = {"seed": 0}

    def next_id() -> str:
        counter["seed"] += 1
        return new_ulid(timestamp_ms=1_700_000_000_000, entropy=bytes([counter["seed"]]) * 10)

    return InMemoryAccountStore(
        box,
        id_factory=next_id,
        clock=lambda: datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )


def add_mock_account(store: InMemoryAccountStore, label: str = "primary") -> Account:
    """Add a mock account (mock accounts need no key)."""
    return store.add(owner_id="user-001", provider=AccountProvider.MOCK, label=label)


# --------------------------------------------------------------------------- crypto
def test_seal_and_open_round_trip(box: SecretBox) -> None:
    """A sealed blob opens back to the exact plaintext."""
    sealed = box.seal(FAKE_KEY)
    assert sealed.startswith(f"{CIPHER_VERSION}:")
    assert box.open(sealed) == FAKE_KEY


def test_sealing_is_randomised(box: SecretBox) -> None:
    """Two seals of the same key differ: the nonce is fresh every time."""
    assert box.seal(FAKE_KEY) != box.seal(FAKE_KEY)


def test_another_secret_cannot_open_the_blob(box: SecretBox) -> None:
    """A blob sealed with one secret is useless to another."""
    sealed = box.seal(FAKE_KEY)
    with pytest.raises(SecretBoxError):
        SecretBox("a-different-store-secret").open(sealed)


def test_tampered_ciphertext_is_refused(box: SecretBox) -> None:
    """A modified blob fails authentication instead of opening to garbage."""
    sealed = box.seal(FAKE_KEY)
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretBoxError):
        box.open(tampered)


def test_unknown_ciphertext_format_is_refused(box: SecretBox) -> None:
    """A blob in an unknown format is an error, not an empty key."""
    with pytest.raises(SecretBoxError):
        box.open("v9:not-a-real-blob")


def test_an_empty_secret_cannot_build_a_box() -> None:
    """A store without a secret must not pretend to encrypt anything."""
    with pytest.raises(SecretBoxError):
        SecretBox("   ")


# --------------------------------------------------------------------------- store
def test_the_store_keeps_a_ciphertext_not_a_key(store: InMemoryAccountStore) -> None:
    """What is retained is ciphertext; the key never appears in the account."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.ANTHROPIC,
        label="work",
        key=FAKE_KEY,
    )
    assert account.key_ciphertext != FAKE_KEY
    assert FAKE_KEY not in account.key_ciphertext
    assert account.key_ciphertext.startswith(f"{CIPHER_VERSION}:")
    assert find_secrets(account.model_dump_json()) == []


def test_reveal_returns_the_key(store: InMemoryAccountStore) -> None:
    """The one method that hands the key back."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.ANTHROPIC,
        label="work",
        key=FAKE_KEY,
    )
    assert store.reveal(account.id) == FAKE_KEY


def test_the_public_view_carries_a_fingerprint_not_a_key(store: InMemoryAccountStore) -> None:
    """The redacted view proves a key exists without revealing it."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.ANTHROPIC,
        label="work",
        key=FAKE_KEY,
    )
    public = account.public()
    assert len(public.key_fingerprint) == 12
    assert FAKE_KEY not in public.model_dump_json()


def test_rotating_the_key_changes_the_fingerprint(store: InMemoryAccountStore) -> None:
    """A new key means a new fingerprint: the UI can prove the key changed."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.ANTHROPIC,
        label="work",
        key=FAKE_KEY,
    )
    before = account.public().key_fingerprint
    rotated = store.replace_key(account.id, OTHER_FAKE_KEY)
    assert rotated.public().key_fingerprint != before
    assert store.reveal(account.id) == OTHER_FAKE_KEY


def test_disabling_an_account_takes_effect_immediately(store: InMemoryAccountStore) -> None:
    """A disabled account is not usable, and the change is visible at once."""
    account = add_mock_account(store)
    assert account.is_usable
    disabled = store.set_status(account.id, AccountStatus.DISABLED)
    assert not disabled.is_usable
    assert store.require(account.id).status is AccountStatus.DISABLED


def test_a_live_provider_needs_a_key(store: InMemoryAccountStore) -> None:
    """A live account without a key is refused instead of stored half-configured."""
    with pytest.raises(AccountStoreError):
        store.add(
            owner_id="user-001",
            provider=AccountProvider.ANTHROPIC,
            label="work",
        )


def test_a_mock_account_may_be_added_without_a_key(store: InMemoryAccountStore) -> None:
    """The mock ignores key material, so the key is optional for it."""
    account = add_mock_account(store)
    assert account.provider is AccountProvider.MOCK
    assert account.is_usable


def test_accounts_can_be_listed_and_filtered(store: InMemoryAccountStore) -> None:
    """The store can answer "which accounts does this owner have?"."""
    add_mock_account(store, "primary")
    store.add(
        owner_id="user-002",
        provider=AccountProvider.ANTHROPIC,
        label="other",
        key=FAKE_KEY,
    )
    assert len(store.list_accounts()) == 2
    assert len(store.list_accounts(owner_id="user-001")) == 1
    assert len(store.list_accounts(provider=AccountProvider.ANTHROPIC)) == 1
    assert store.list_accounts(provider=AccountProvider.MOCK)[0].label == "primary"


def test_deleting_an_account_removes_it(store: InMemoryAccountStore) -> None:
    """Deletion is immediate: a stale binding must not find the account again."""
    account = add_mock_account(store)
    store.delete(account.id)
    assert store.get(account.id) is None
    with pytest.raises(AccountNotFound):
        store.require(account.id)


def test_usage_is_counted_per_account_and_period(store: InMemoryAccountStore) -> None:
    """The monthly budget is measured in tokens, per account, per period."""
    account = add_mock_account(store)
    store.record_usage(account.id, 1_000)
    store.record_usage(account.id, 500)
    assert store.usage(account.id) == 1_500
    store.record_usage(account.id, 0)
    assert store.usage(account.id) == 1_500


def test_remaining_reflects_the_monthly_budget(store: InMemoryAccountStore) -> None:
    """``remaining`` is the headroom left in the account's own budget."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.MOCK,
        label="budgeted",
        monthly_budget=MonthlyBudget(tokens=10_000),
    )
    assert store.remaining(account) == 10_000
    store.record_usage(account.id, 2_500)
    assert store.remaining(account) == 7_500
    assert store.remaining(store.require(account.id)) == 7_500


def test_metadata_can_be_changed_without_touching_the_key(store: InMemoryAccountStore) -> None:
    """Label and budget are editable; the key is not reachable from here."""
    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.ANTHROPIC,
        label="work",
        key=FAKE_KEY,
    )
    updated = store.update_metadata(
        account.id,
        label="work-2",
        monthly_budget=MonthlyBudget(tokens=5_000),
    )
    assert updated.label == "work-2"
    assert updated.monthly_budget.tokens == 5_000
    assert store.reveal(account.id) == FAKE_KEY
