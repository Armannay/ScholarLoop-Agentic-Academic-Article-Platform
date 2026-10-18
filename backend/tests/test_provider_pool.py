"""The provider pool: bindings and accounts are resolved *now*, not cached.

The pool is where ADR-0004 becomes behaviour, so the tests are about time:

* a binding that changes between two calls is used by the second call;
* an account that is deleted between two calls is not used by the second call;
* a failing account is followed by the next one in the binding;
* the key is decrypted at call time and handed to the builder - the only place it
  may go.
"""

from __future__ import annotations

import pytest

from app.accounts.crypto import SecretBox
from app.accounts.store import InMemoryAccountStore
from app.contracts.account import Account
from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountProvider, AccountStatus, BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.core.errors import BudgetExceeded
from app.core.policies import Policies
from app.governor.budget import TokenGovernor
from app.providers.base import LLMProvider
from app.providers.errors import (
    NoAccountAvailable,
    ProviderTimeout,
    ProviderUnsupported,
)
from app.providers.mock import MockProvider, Script
from app.providers.pool import BindingRegistry, NoBindingAvailable, ProviderPool
from tests.factories import sample_ulid

pytestmark = pytest.mark.providers

FAKE_KEY = "unit-" + "test-" + "key-" + "0123456789abcdef"
STORE_SECRET = "unit-test-store-secret-not-a-real-secret"


class ScriptedBuilder:
    """A provider builder that hands out a scripted provider per account.

    It also records what it was given, which is how the tests prove that the key
    is decrypted at call time and handed to the builder - and nowhere else.
    """

    def __init__(self, providers: dict[str, LLMProvider]) -> None:
        self._providers = providers
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, account: Account, *, key: str | None = None) -> LLMProvider:
        self.calls.append((account.id, key))
        provider = self._providers.get(account.id)
        if provider is None:
            raise AssertionError(f"no provider scripted for account {account.id}")
        return provider


def make_policies() -> Policies:
    """Policies with room for the test calls, but with the caps spelled out."""
    return Policies(run_token_cap=50_000, iteration_token_cap=20_000, call_input_cap=4_000)


def make_request(text: str = "Draft the introduction.") -> LLMRequest:
    """A small, valid request."""
    return LLMRequest(
        system_blocks=[{"text": "You are the writer agent."}],
        messages=[{"role": "user", "text": text}],
        max_output_tokens=512,
    )


def make_binding(
    account_ids: list[str],
    *,
    role: BindingRole = BindingRole.WRITER,
    strategy: str = "dedicated",
    model: str = "test-model",
) -> AgentBinding:
    """A binding over the given accounts."""
    return AgentBinding(
        role=role,
        account_ids=account_ids,
        strategy=strategy,
        model=model,
        max_input_tokens=24_000,
        max_output_tokens=8_000,
    )


@pytest.fixture
def store() -> InMemoryAccountStore:
    """An in-memory store with deterministic ids."""
    counter = {"seed": 0}

    def next_id() -> str:
        counter["seed"] += 1
        return sample_ulid(counter["seed"])

    return InMemoryAccountStore(
        SecretBox(STORE_SECRET),
        id_factory=next_id,
    )


def add_account(store: InMemoryAccountStore, label: str, provider: AccountProvider) -> Account:
    """Add an account of the given provider."""
    return store.add(
        owner_id="user-001",
        provider=provider,
        label=label,
        key=None if provider is AccountProvider.MOCK else FAKE_KEY,
    )


# --------------------------------------------------------------------------- registry
def test_a_missing_binding_is_a_user_condition() -> None:
    """No binding for the role: the loop asks the user, it does not crash."""
    registry = BindingRegistry()
    with pytest.raises(NoBindingAvailable):
        registry.resolve("writer")


def test_a_binding_can_be_disabled_and_enabled_again() -> None:
    """Disabling a role is how an operator stops a spend."""
    registry = BindingRegistry()
    registry.upsert(make_binding([sample_ulid(1)]))
    registry.disable("writer")
    with pytest.raises(NoBindingAvailable):
        registry.resolve("writer")
    registry.enable("writer")
    assert registry.resolve("writer").is_active


def test_the_registry_lists_its_roles() -> None:
    """The registry knows which roles are configured."""
    registry = BindingRegistry()
    registry.upsert(make_binding([sample_ulid(1)], role=BindingRole.WRITER))
    registry.upsert(make_binding([sample_ulid(2)], role=BindingRole.REVIEWER))
    assert registry.roles() == ["reviewer", "writer"]


# --------------------------------------------------------------------------- pool
async def test_a_call_uses_the_binding_of_the_moment(store: InMemoryAccountStore) -> None:
    """A binding that changed between two calls is used by the second call."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    provider = MockProvider(Script().reply(BindingRole.WRITER, 0, '{"ok": true}'))
    builder = ScriptedBuilder({account.id: provider})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id], model="first-model"))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    await pool.call(make_request(), role=BindingRole.WRITER)

    registry.upsert(make_binding([account.id], model="second-model"))
    await pool.call(make_request(), role=BindingRole.WRITER)

    assert [call.model for call in provider.calls] == [
        "first-model",
        "first-model",
        "second-model",
    ]


async def test_a_deleted_account_is_not_used_again(store: InMemoryAccountStore) -> None:
    """A binding that points at a deleted account must not resurrect it."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=ScriptedBuilder({}))

    store.delete(account.id)
    with pytest.raises(NoAccountAvailable):
        await pool.call(make_request(), role=BindingRole.WRITER)


async def test_failover_moves_to_the_next_account(store: InMemoryAccountStore) -> None:
    """A failing account is followed by the next one in the binding."""
    first = add_account(store, "first", AccountProvider.MOCK)
    second = add_account(store, "second", AccountProvider.MOCK)

    failing = MockProvider(Script().fail(BindingRole.WRITER, 0, "timeout"))
    working = MockProvider(Script().reply(BindingRole.WRITER, 0, '{"ok": true}'))
    builder = ScriptedBuilder({first.id: failing, second.id: working})

    registry = BindingRegistry()
    registry.upsert(make_binding([first.id, second.id], strategy="failover"))
    pool = ProviderPool(store, registry, builder=builder)

    response = await pool.call(make_request(), role=BindingRole.WRITER)
    assert response.text == '{"ok": true}'
    assert [account_id for account_id, _ in builder.calls] == [first.id, second.id]


async def test_every_candidate_failing_raises_the_last_error(store: InMemoryAccountStore) -> None:
    """When no account works, the caller learns why."""
    first = add_account(store, "first", AccountProvider.MOCK)
    second = add_account(store, "second", AccountProvider.MOCK)
    failing = MockProvider(Script().fail(BindingRole.WRITER, 0, "timeout", times=5))
    builder = ScriptedBuilder({first.id: failing, second.id: failing})

    registry = BindingRegistry()
    registry.upsert(make_binding([first.id, second.id], strategy="failover"))
    pool = ProviderPool(store, registry, builder=builder)

    with pytest.raises(ProviderTimeout):
        await pool.call(make_request(), role=BindingRole.WRITER)


async def test_a_disabled_account_is_skipped(store: InMemoryAccountStore) -> None:
    """Disabling an account takes effect on the very next call."""
    first = add_account(store, "first", AccountProvider.MOCK)
    second = add_account(store, "second", AccountProvider.MOCK)
    store.set_status(first.id, AccountStatus.DISABLED)

    working = MockProvider(Script().reply(BindingRole.WRITER, 0, '{"ok": true}'))
    builder = ScriptedBuilder({second.id: working})
    registry = BindingRegistry()
    registry.upsert(make_binding([first.id, second.id], strategy="failover"))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    assert [account_id for account_id, _ in builder.calls] == [second.id]


async def test_round_robin_rotates_the_accounts(store: InMemoryAccountStore) -> None:
    """Round robin spreads the load over the accounts of a binding."""
    first = add_account(store, "first", AccountProvider.MOCK)
    second = add_account(store, "second", AccountProvider.MOCK)
    builder = ScriptedBuilder(
        {
            first.id: MockProvider(Script().reply(BindingRole.WRITER, 0, "a")),
            second.id: MockProvider(Script().reply(BindingRole.WRITER, 0, "b")),
        }
    )
    registry = BindingRegistry()
    registry.upsert(make_binding([first.id, second.id], strategy="round_robin"))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    await pool.call(make_request(), role=BindingRole.WRITER)

    assert [account_id for account_id, _ in builder.calls] == [first.id, second.id]


async def test_dedicated_uses_the_first_account_only(store: InMemoryAccountStore) -> None:
    """A dedicated binding never wanders to a second account."""
    first = add_account(store, "first", AccountProvider.MOCK)
    second = add_account(store, "second", AccountProvider.MOCK)
    builder = ScriptedBuilder(
        {
            first.id: MockProvider(Script().reply(BindingRole.WRITER, 0, "a")),
            second.id: MockProvider(Script().reply(BindingRole.WRITER, 0, "b")),
        }
    )
    registry = BindingRegistry()
    registry.upsert(make_binding([first.id, second.id], strategy="dedicated"))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    await pool.call(make_request(), role=BindingRole.WRITER)

    assert [account_id for account_id, _ in builder.calls] == [first.id, first.id]


async def test_the_key_is_decrypted_at_call_time(store: InMemoryAccountStore) -> None:
    """The builder receives the plaintext key; the pool keeps nothing."""
    account = add_account(store, "live", AccountProvider.ANTHROPIC)
    builder = ScriptedBuilder({account.id: MockProvider(Script())})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    assert builder.calls == [(account.id, FAKE_KEY)]


async def test_a_mock_account_never_sees_a_key(store: InMemoryAccountStore) -> None:
    """The mock ignores key material, so none is decrypted for it."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    builder = ScriptedBuilder({account.id: MockProvider(Script())})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)
    assert builder.calls == [(account.id, None)]


async def test_the_usage_is_charged_to_the_governor_and_the_account(
    store: InMemoryAccountStore,
) -> None:
    """A finished call is billed to the run and to the account."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    provider = MockProvider(Script().reply(BindingRole.WRITER, 0, "a short answer"))
    builder = ScriptedBuilder({account.id: provider})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    governor = TokenGovernor(make_policies())
    pool = ProviderPool(store, registry, governor=governor, builder=builder)

    await pool.call(make_request(), role=BindingRole.WRITER)

    assert governor.run_used == provider.total_tokens().total
    assert store.usage(account.id) == provider.total_tokens().total


async def test_the_governor_refuses_a_call_that_does_not_fit(store: InMemoryAccountStore) -> None:
    """The pool asks the governor before it spends anything."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    builder = ScriptedBuilder({account.id: MockProvider(Script())})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    governor = TokenGovernor(make_policies())
    governor.charge(TokenUsage(in_=20_000, out=0, cached=0))
    pool = ProviderPool(store, registry, governor=governor, builder=builder)

    with pytest.raises(BudgetExceeded):
        await pool.call(make_request(), role=BindingRole.WRITER)


async def test_an_account_without_budget_is_not_called(store: InMemoryAccountStore) -> None:
    """The account's own monthly budget is a hard stop."""
    from app.contracts.account import MonthlyBudget

    account = store.add(
        owner_id="user-001",
        provider=AccountProvider.MOCK,
        label="budgeted",
        monthly_budget=MonthlyBudget(tokens=1_000),
    )
    builder = ScriptedBuilder({account.id: MockProvider(Script())})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=builder)

    with pytest.raises(BudgetExceeded) as excinfo:
        await pool.call(make_request("x" * 4_000), role=BindingRole.WRITER)
    assert excinfo.value.context["cap"] == "account_monthly_budget"
    assert builder.calls == []


async def test_an_exhausted_account_gives_way_to_the_next_one(
    store: InMemoryAccountStore,
) -> None:
    """A binding may mix a spent account with one that can still pay."""
    from app.contracts.account import MonthlyBudget

    spent = store.add(
        owner_id="user-001",
        provider=AccountProvider.MOCK,
        label="spent",
        monthly_budget=MonthlyBudget(tokens=1_000),
    )
    funded = add_account(store, "funded", AccountProvider.MOCK)
    builder = ScriptedBuilder(
        {funded.id: MockProvider(Script().reply(BindingRole.WRITER, 0, "ok"))}
    )
    registry = BindingRegistry()
    registry.upsert(make_binding([spent.id, funded.id], strategy="failover"))
    pool = ProviderPool(store, registry, builder=builder)

    response = await pool.call(make_request("x" * 4_000), role=BindingRole.WRITER)
    assert response.text == "ok"
    assert [account_id for account_id, _ in builder.calls] == [funded.id]


def test_the_pool_resolves_a_binding_like_the_routing_protocol(
    store: InMemoryAccountStore,
) -> None:
    """``resolve_binding`` is the call-time lookup the agent runtime uses."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=ScriptedBuilder({}))

    assert pool.resolve_binding("writer").role is BindingRole.WRITER


def test_get_provider_builds_one(store: InMemoryAccountStore) -> None:
    """``get_provider`` is the seam the agent runtime uses for a provider."""
    account = add_account(store, "primary", AccountProvider.MOCK)
    builder = ScriptedBuilder({account.id: MockProvider(Script())})
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry, builder=builder)

    provider = pool.get_provider(pool.resolve_binding("writer"))
    assert isinstance(provider, MockProvider)


def test_a_live_account_without_an_adapter_is_a_dead_end(store: InMemoryAccountStore) -> None:
    """No adapter yet: the shipped builder refuses instead of guessing."""
    account = add_account(store, "live", AccountProvider.ANTHROPIC)
    registry = BindingRegistry()
    registry.upsert(make_binding([account.id]))
    pool = ProviderPool(store, registry)

    with pytest.raises(ProviderUnsupported):
        pool.get_provider(pool.resolve_binding("writer"))
