"""ProviderPool: call-time resolution, strategies, circuit breaker, hot-swap, usage.

The pool is driven with fake resolvers and stub providers so every behaviour is
observable without HTTP: which account was chosen, which *key* the provider was
built with (the hot-swap proof), and what usage record was emitted.

The hot-swap test is the step's core promise: a key rotated mid-run leaves
in-flight calls running on the old provider and sends every new call to the new
one - no restart, no half-used credentials.
"""

from __future__ import annotations

import asyncio

import pytest

from app.contracts.account import Account, MonthlyBudget
from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountProvider, AccountStatus, BindingStrategy
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.providers.errors import (
    NoAccountAvailable,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.pool import BreakerState, CircuitBreaker, ProviderPool, UsageRecord
from tests.factories import make_request, sample_ulid, sample_ulids

pytestmark = pytest.mark.providers


# --------------------------------------------------------------------------- fakes
class StubProvider:
    """Scriptable LLMProvider stand-in that records the key it was built with."""

    name = "stub"

    def __init__(
        self,
        key: str,
        *,
        results: list[object] | None = None,
        gate: asyncio.Event | None = None,
        call_log: list[str] | None = None,
    ) -> None:
        self.key = key
        self.results = list(results or [])
        self.gate = gate
        self.call_log = call_log
        self.calls = 0
        self.closed = False

    async def generate(self, request: LLMRequest, *, model=None, role=None, account_id=None):
        self.calls += 1
        if self.call_log is not None:
            self.call_log.append(self.key)
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.results.pop(0) if self.results else None
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, LLMResponse):
            return outcome
        return LLMResponse(
            text="ok",
            usage=TokenUsage(**{"in": 10, "out": 5, "cached": 2}),
            model=model or "stub-model",
            latency_ms=1,
        )

    async def count_tokens(self, request):
        return 1

    async def aclose(self) -> None:
        self.closed = True


class FakeResolver:
    """In-memory AccountResolver + status-change recorder."""

    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], AgentBinding] = {}
        self.accounts: dict[str, tuple[Account, str]] = {}
        self.status_changes: list[tuple[str, AccountStatus, str]] = []

    def add_account(
        self,
        account_id: str,
        key: str,
        *,
        status: AccountStatus = AccountStatus.ACTIVE,
        provider: AccountProvider = AccountProvider.MOCK,
        base_url: str | None = None,
    ) -> None:
        account = Account(
            id=account_id,
            owner_id=sample_ulid(9),
            provider=provider,
            label="stub",
            key_ciphertext="v1.stub.stub.stub.stub",
            base_url=base_url,
            status=status,
            monthly_budget=MonthlyBudget(),
        )
        self.accounts[account_id] = (account, key)

    def set_key(self, account_id: str, key: str) -> None:
        account, _ = self.accounts[account_id]
        self.accounts[account_id] = (account, key)

    def set_base_url(self, account_id: str, base_url: str) -> None:
        account, key = self.accounts[account_id]
        self.accounts[account_id] = (account.model_copy(update={"base_url": base_url}), key)

    def bind(self, user_id: str, role: str, account_ids: list[str], **kwargs) -> AgentBinding:
        binding = AgentBinding(
            role=role,
            account_ids=account_ids,
            strategy=kwargs.pop("strategy", BindingStrategy.FAILOVER),
            model=kwargs.pop("model", "stub-model"),
            max_input_tokens=kwargs.pop("max_input_tokens", 24_000),
            max_output_tokens=kwargs.pop("max_output_tokens", 4_000),
            enabled=kwargs.pop("enabled", True),
        )
        self.bindings[(user_id, role)] = binding
        return binding

    def load_for_call(self, account_id: str):
        try:
            account, key = self.accounts[account_id]
        except KeyError as exc:
            raise NoAccountAvailable(f"account {account_id} gone") from exc
        if account.status is AccountStatus.DISABLED:
            raise NoAccountAvailable(f"account {account_id} disabled")
        return Resolved(account, key)

    def get_binding(self, user_id: str, role: str):
        return self.bindings.get((user_id, role))

    def set_account_status(
        self, account_id: str, status: AccountStatus, *, actor: str, reason: str
    ):
        account, key = self.accounts[account_id]
        self.accounts[account_id] = (
            Account(
                id=account.id,
                owner_id=account.owner_id,
                provider=account.provider,
                label=account.label,
                key_ciphertext=account.key_ciphertext,
                base_url=account.base_url,
                status=status,
                monthly_budget=account.monthly_budget,
            ),
            key,
        )
        self.status_changes.append((account_id, status, reason))


class Resolved:
    def __init__(self, account: Account, api_key: str) -> None:
        self.account = account
        self.api_key = api_key


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def emit(self, record: UsageRecord) -> None:
        self.records.append(record)


def pool_with(resolver: FakeResolver, *, sink=None, breaker=None, provider_store=None):
    """A pool whose factory records (key -> provider) for assertions."""

    built: list[StubProvider] = provider_store if provider_store is not None else []

    def factory(*, account, api_key):
        provider = StubProvider(api_key)
        built.append(provider)
        return provider

    pool = ProviderPool(resolver, provider_factory=factory, usage_sink=sink, breaker=breaker)
    return pool, built


USER = sample_ulid(1)
IDS = sample_ulids(4)


# --------------------------------------------------------------------------- resolution
@pytest.mark.asyncio
async def test_account_is_resolved_at_call_time_not_at_pool_build() -> None:
    resolver = FakeResolver()
    pool, _ = pool_with(resolver)
    request = make_request()
    with pytest.raises(NoAccountAvailable):
        await pool.generate(request, role="writer", user_id=USER)
    # The binding and account appear *after* the pool exists: still picked up.
    resolver.add_account(IDS[0], "key-late")
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)
    response = await pool.generate(request, role="writer", user_id=USER)
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_dedicated_strategy_uses_the_first_account_only() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "architect", [IDS[0], IDS[1]], strategy=BindingStrategy.DEDICATED)
    pool, built = pool_with(resolver)
    for _ in range(3):
        await pool.generate(make_request(), role="architect", user_id=USER)
    assert [p.key for p in built] == ["k0"]


@pytest.mark.asyncio
async def test_round_robin_rotates_across_accounts() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "writer", [IDS[0], IDS[1]], strategy=BindingStrategy.ROUND_ROBIN)
    call_order: list[str] = []

    def factory(*, account, api_key):
        return StubProvider(api_key, call_log=call_order)

    pool = ProviderPool(resolver, provider_factory=factory)
    for _ in range(4):
        await pool.generate(make_request(), role="writer", user_id=USER)
    # Providers are cached per account; rotation shows up in the call order.
    assert call_order == ["k0", "k1", "k0", "k1"]


@pytest.mark.asyncio
async def test_failover_moves_to_the_next_account_on_retryable_error() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "reviewer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)
    request = make_request()
    first_provider = StubProvider("k0", results=[ProviderRateLimited("slow down")])
    second_provider = StubProvider("k1")

    def factory(*, account, api_key):
        return first_provider if api_key == "k0" else second_provider

    pool = ProviderPool(resolver, provider_factory=factory)
    response = await pool.generate(request, role="reviewer", user_id=USER)
    assert response.text == "ok"
    assert first_provider.calls == 1
    assert second_provider.calls == 1


@pytest.mark.asyncio
async def test_failover_raises_last_error_when_every_account_fails() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "reviewer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)

    def factory(*, account, api_key):
        return StubProvider(api_key, results=[ProviderUnavailable(f"down {api_key}")])

    pool = ProviderPool(resolver, provider_factory=factory)
    with pytest.raises(ProviderUnavailable, match="down k1"):
        await pool.generate(make_request(), role="reviewer", user_id=USER)


@pytest.mark.asyncio
async def test_bad_response_does_not_fail_over() -> None:
    """A malformed answer is a property of the call: don't burn other credentials."""
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "reviewer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)

    def factory(*, account, api_key):
        return StubProvider(api_key, results=[ProviderBadResponse("garbage")])

    pool = ProviderPool(resolver, provider_factory=factory)
    with pytest.raises(ProviderBadResponse):
        await pool.generate(make_request(), role="reviewer", user_id=USER)


@pytest.mark.asyncio
async def test_disabled_binding_and_disabled_account_raise_no_account() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0", status=AccountStatus.DISABLED)
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)
    pool, _ = pool_with(resolver)
    with pytest.raises(NoAccountAvailable):
        await pool.generate(make_request(), role="writer", user_id=USER)
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "writer", [IDS[1]], strategy=BindingStrategy.DEDICATED, enabled=False)
    with pytest.raises(NoAccountAvailable):
        await pool.generate(make_request(), role="writer", user_id=USER)


# --------------------------------------------------------------------------- breaker
@pytest.mark.asyncio
async def test_circuit_breaker_opens_marks_degraded_and_recovers() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "writer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)

    clock = {"now": 1000.0}
    breaker = CircuitBreaker(
        failure_threshold=2, recheck_interval_s=60.0, clock=lambda: clock["now"]
    )
    failing = StubProvider("k0", results=[ProviderAuthError("revoked")] * 5)
    healthy = StubProvider("k1")

    def factory(*, account, api_key):
        return failing if api_key == "k0" else healthy

    pool = ProviderPool(resolver, provider_factory=factory, breaker=breaker)
    request = make_request()

    # Two failures on k0 (each call fails over to k1 and succeeds overall).
    await pool.generate(request, role="writer", user_id=USER)
    await pool.generate(request, role="writer", user_id=USER)
    assert breaker.state(IDS[0]) is BreakerState.OPEN
    assert any(s[0] == IDS[0] and s[1] is AccountStatus.DEGRADED for s in resolver.status_changes)

    # While open, k0 is skipped entirely.
    healthy_calls_before = healthy.calls
    await pool.generate(request, role="writer", user_id=USER)
    assert healthy.calls == healthy_calls_before + 1
    assert failing.calls == 2

    # After the re-check interval a probe is allowed; success closes and recovers.
    clock["now"] += 61.0
    failing.results = []  # the probe succeeds
    await pool.generate(request, role="writer", user_id=USER)
    assert breaker.state(IDS[0]) is BreakerState.CLOSED
    assert resolver.status_changes[-1][1] is AccountStatus.ACTIVE


def test_breaker_half_open_admits_exactly_one_probe() -> None:
    clock = {"now": 0.0}
    breaker = CircuitBreaker(
        failure_threshold=1, recheck_interval_s=10.0, clock=lambda: clock["now"]
    )
    breaker.record_failure("a")
    assert breaker.allow("a") is False
    clock["now"] += 11.0
    assert breaker.allow("a") is True  # the probe
    assert breaker.allow("a") is False  # everyone else waits
    breaker.record_failure("a")  # probe failed: back to open, timer restarts
    assert breaker.state("a") is BreakerState.OPEN
    assert breaker.allow("a") is False
    clock["now"] += 11.0
    assert breaker.allow("a") is True
    breaker.record_success("a")
    assert breaker.state("a") is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_unused_candidate_keeps_its_half_open_probe() -> None:
    """A success on A must not spend B's probe when B was never called.

    Regression: the pool asked the breaker about every candidate *before* the
    loop, so B's half-open probe was claimed and left in flight; B then stayed
    un-probeable for the rest of the process.
    """
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "writer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)

    clock = {"now": 0.0}
    breaker = CircuitBreaker(
        failure_threshold=1, recheck_interval_s=10.0, clock=lambda: clock["now"]
    )
    breaker.record_failure(IDS[0])
    breaker.record_failure(IDS[1])
    clock["now"] += 11.0

    pool, _ = pool_with(resolver, breaker=breaker)
    await pool.generate(make_request(), role="writer", user_id=USER)
    assert breaker.state(IDS[0]) is BreakerState.CLOSED
    # B was never asked about, so its probe slot was never claimed.
    assert breaker.state(IDS[1]) is BreakerState.OPEN

    # A is gone; B's probe must still be available, or the role is dead.
    resolver.accounts.pop(IDS[0])
    resolver.bind(USER, "writer", [IDS[1]], strategy=BindingStrategy.DEDICATED)
    await pool.generate(make_request(), role="writer", user_id=USER)
    assert breaker.state(IDS[1]) is BreakerState.CLOSED


def test_breaker_releases_a_probe_that_never_reached_a_provider() -> None:
    """If resolution fails, the claimed half-open probe must be given back."""
    clock = {"now": 0.0}
    breaker = CircuitBreaker(
        failure_threshold=1, recheck_interval_s=10.0, clock=lambda: clock["now"]
    )
    breaker.record_failure("a")
    clock["now"] += 11.0
    assert breaker.allow("a") is True  # probe claimed
    assert breaker.allow("a") is False
    breaker.release_probe("a")  # account vanished before any provider call
    assert breaker.allow("a") is True  # next call may probe again
    breaker.record_success("a")
    assert breaker.state("a") is BreakerState.CLOSED


# --------------------------------------------------------------------------- hot-swap
@pytest.mark.asyncio
async def test_changing_base_url_rebuilds_the_provider() -> None:
    """An OpenAI-compatible account keeps its key but may move endpoints.

    The pool caches per (account, key, base_url); a key-only cache would keep
    sending calls to the old host after a PATCH changed ``base_url``.
    """
    resolver = FakeResolver()
    resolver.add_account(
        IDS[0],
        "k0",
        provider=AccountProvider.OPENAI_COMPATIBLE,
        base_url="https://old.example/v1",
    )
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)
    seen: list[tuple[str, str | None]] = []

    def factory(*, account, api_key):
        seen.append((api_key, account.base_url))
        return StubProvider(api_key)

    pool = ProviderPool(resolver, provider_factory=factory)
    await pool.generate(make_request(), role="writer", user_id=USER)
    resolver.set_base_url(IDS[0], "https://new.example/v1")
    await pool.generate(make_request(), role="writer", user_id=USER)
    assert seen == [("k0", "https://old.example/v1"), ("k0", "https://new.example/v1")]


@pytest.mark.asyncio
async def test_round_robin_cursor_is_per_user_and_role() -> None:
    """Two users with the same role must not share (and advance) one cursor."""
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    first_user, second_user = sample_ulid(1), sample_ulid(2)
    resolver.bind(first_user, "writer", [IDS[0], IDS[1]], strategy=BindingStrategy.ROUND_ROBIN)
    resolver.bind(second_user, "writer", [IDS[0], IDS[1]], strategy=BindingStrategy.ROUND_ROBIN)
    order: list[str] = []

    def factory(*, account, api_key):
        return StubProvider(api_key, call_log=order)

    pool = ProviderPool(resolver, provider_factory=factory)
    await pool.generate(make_request(), role="writer", user_id=first_user)
    await pool.generate(make_request(), role="writer", user_id=first_user)
    await pool.generate(make_request(), role="writer", user_id=second_user)
    assert order == ["k0", "k1", "k0"]


@pytest.mark.asyncio
async def test_unbuildable_account_fails_over_and_does_not_poison_the_pool() -> None:
    """A per-account adapter error is a candidate failure, not a whole-call failure."""
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.add_account(IDS[1], "k1")
    resolver.bind(USER, "reviewer", [IDS[0], IDS[1]], strategy=BindingStrategy.FAILOVER)

    def factory(*, account, api_key):
        if api_key == "k0":
            raise ProviderUnsupported("no adapter for this configuration")
        return StubProvider(api_key)

    pool = ProviderPool(resolver, provider_factory=factory)
    response = await pool.generate(make_request(), role="reviewer", user_id=USER)
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_hot_swap_in_flight_calls_finish_on_the_old_key() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "key-OLD")
    resolver.bind(USER, "planner", [IDS[0]], strategy=BindingStrategy.DEDICATED)

    gate = asyncio.Event()
    old_provider = StubProvider("key-OLD", gate=gate)
    new_provider_box: list[StubProvider] = []

    def factory(*, account, api_key):
        if api_key == "key-OLD":
            return old_provider
        provider = StubProvider(api_key)
        new_provider_box.append(provider)
        return provider

    pool = ProviderPool(resolver, provider_factory=factory)
    request = make_request()

    # Call 1 starts and parks inside generate() on the OLD key.
    in_flight = asyncio.create_task(pool.generate(request, role="planner", user_id=USER))
    await asyncio.sleep(0)  # let it reach the gate
    assert old_provider.calls == 1

    # The user rotates the key mid-run.
    resolver.set_key(IDS[0], "key-NEW")

    # Call 2 arrives while call 1 is still in flight: brand-new provider, new key.
    response2 = await pool.generate(request, role="planner", user_id=USER)
    assert response2.text == "ok"
    assert new_provider_box and new_provider_box[0].key == "key-NEW"

    # Call 1 completes on the old provider, unaffected by the swap.
    gate.set()
    response1 = await in_flight
    assert response1.text == "ok"
    assert old_provider.calls == 1

    # Call 3 keeps using the new key; the old provider is retired.
    await pool.generate(request, role="planner", user_id=USER)
    assert old_provider.calls == 1
    assert new_provider_box[0].calls == 2
    await pool.aclose()
    assert old_provider.closed is True


@pytest.mark.asyncio
async def test_evict_forgets_the_cached_provider() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)
    pool, built = pool_with(resolver)
    await pool.generate(make_request(), role="writer", user_id=USER)
    assert len(built) == 1
    pool.evict(IDS[0])
    await pool.generate(make_request(), role="writer", user_id=USER)
    assert len(built) == 2  # a fresh provider after eviction


# --------------------------------------------------------------------------- usage
@pytest.mark.asyncio
async def test_usage_records_emitted_for_success_and_failure() -> None:
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)
    sink = RecordingSink()
    ok_provider = StubProvider("k0")

    def factory(*, account, api_key):
        return ok_provider

    pool = ProviderPool(resolver, provider_factory=factory, usage_sink=sink)
    await pool.generate(make_request(), role="writer", user_id=USER)
    ok_provider.results = [ProviderUnavailable("down")]
    with pytest.raises(ProviderUnavailable):
        await pool.generate(make_request(), role="writer", user_id=USER)

    assert len(sink.records) == 2
    good, bad = sink.records
    assert (
        good.ok is True
        and good.tokens_in == 10
        and good.tokens_out == 5
        and good.tokens_cached == 2
    )
    assert good.role == "writer" and good.account_id == IDS[0] and good.model == "stub-model"
    assert good.est_cost >= 0.0
    assert bad.ok is False and bad.tokens_in == 0 and bad.error_code == "provider_unavailable"
    assert good.ts is not None


@pytest.mark.asyncio
async def test_usage_sink_failure_does_not_fail_the_provider_call() -> None:
    """Accounting is downstream of the spend; a full audit table must not lose a reply."""
    resolver = FakeResolver()
    resolver.add_account(IDS[0], "k0")
    resolver.bind(USER, "writer", [IDS[0]], strategy=BindingStrategy.DEDICATED)

    class ExplodingSink:
        def emit(self, record: UsageRecord) -> None:
            raise RuntimeError("usage table unavailable")

    pool = ProviderPool(
        resolver,
        provider_factory=lambda *, account, api_key: StubProvider(api_key),
        usage_sink=ExplodingSink(),
    )
    response = await pool.generate(make_request(), role="writer", user_id=USER)
    assert response.text == "ok"
