"""The provider pool: resolve a binding and an account *now*, then call.

ADR-0004 in code. A binding names account ids; the pool resolves them at call
time, so a key that was rotated, disabled or deleted five minutes ago is the one
that matters right now. Nothing above this module may hold a provider instance or
a key across a call boundary.

The pool is also where the three services meet:

* the **binding registry** answers "which accounts may this role use?";
* the **account store** answers "which of them exist, are usable and still have
  budget?" and decrypts the key;
* the **governor** answers "may this call happen at all?" and is charged for it
  afterwards.

Failover is a property of the *binding*, not of the pool: ``failover`` walks the
account list in order until one call succeeds, ``round_robin`` rotates the start
position, ``dedicated`` uses the first account only. A call that fails for a
*provider* reason (timeout, rate limit, revoked key) moves on to the next account.
An account that is out of its *own* monthly budget is skipped as well - the next
account may still be able to pay. A call that does not fit into the run's or the
iteration's budget is refused before the loop starts, because that is a property
of the run and not of an account.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.accounts.store import AccountStore
from app.contracts.account import Account
from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountProvider, BindingRole, BindingStrategy
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.core.errors import BudgetExceeded
from app.governor.budget import TokenGovernor
from app.providers.base import LLMProvider, prompt_tokens
from app.providers.errors import NoAccountAvailable, ProviderError
from app.providers.factory import build_provider

__all__ = [
    "BindingRegistry",
    "NoBindingAvailable",
    "ProviderBuilder",
    "ProviderPool",
]


class NoBindingAvailable(NoAccountAvailable):
    """No binding is configured for the requested role.

    A user condition, like :class:`NoAccountAvailable`: the loop turns it into
    ``needs_user_input`` rather than failing the run.
    """

    code = "no_binding_available"
    http_status = 409


class ProviderBuilder(Protocol):
    """Builds a provider for one account.

    ``key`` is the decrypted provider key. It is passed *here* and nowhere else:
    a builder is the only component that may hold a key, and it must not keep it.
    """

    def __call__(self, account: Account, *, key: str | None = None) -> LLMProvider: ...


def default_builder(account: Account, *, key: str | None = None) -> LLMProvider:
    """Build the shipped adapter for an account.

    The mock ignores key material entirely, and the live adapters do not exist
    yet (:func:`app.providers.factory.build_provider` raises
    :class:`~app.providers.errors.ProviderUnsupported` for them). The ``key`` is
    accepted here so that a live adapter can be plugged in without changing the
    pool.
    """
    _ = key
    return build_provider(account)


class BindingRegistry:
    """Call-time binding resolution: role -> :class:`AgentBinding`.

    The registry holds bindings, never accounts and never keys (see
    :class:`app.contracts.binding.AgentBinding`). It is mutable on purpose:
    users change provider accounts while a run is in flight.
    """

    def __init__(self, bindings: Mapping[str, AgentBinding] | None = None) -> None:
        self._bindings: dict[str, AgentBinding] = {}
        for binding in (bindings or {}).values():
            self.upsert(binding)

    def upsert(self, binding: AgentBinding) -> None:
        """Register (or replace) the binding of a role."""
        self._bindings[binding.role.value] = binding

    def resolve(self, role: str | BindingRole) -> AgentBinding:
        """Return the binding of a role.

        Raises:
            NoBindingAvailable: the role has no binding, or its binding is
                disabled.
        """
        key = role.value if isinstance(role, BindingRole) else str(role)
        binding = self._bindings.get(key)
        if binding is None:
            raise NoBindingAvailable(
                f"no binding is configured for role {key!r}",
                provider="pool",
            )
        if not binding.is_active:
            raise NoBindingAvailable(
                f"the binding for role {key!r} is disabled",
                provider="pool",
            )
        return binding

    def disable(self, role: str | BindingRole) -> None:
        """Disable a role's binding without deleting it."""
        key = role.value if isinstance(role, BindingRole) else str(role)
        binding = self._bindings.get(key)
        if binding is None:
            raise NoBindingAvailable(
                f"no binding is configured for role {key!r}",
                provider="pool",
            )
        self._bindings[key] = binding.model_copy(update={"enabled": False})

    def enable(self, role: str | BindingRole) -> None:
        """Re-enable a role's binding."""
        key = role.value if isinstance(role, BindingRole) else str(role)
        binding = self._bindings.get(key)
        if binding is None:
            raise NoBindingAvailable(
                f"no binding is configured for role {key!r}",
                provider="pool",
            )
        self._bindings[key] = binding.model_copy(update={"enabled": True})

    def roles(self) -> list[str]:
        """The roles that have a binding, sorted."""
        return sorted(self._bindings)

    def __contains__(self, role: object) -> bool:
        """True when a binding is registered for ``role``."""
        return isinstance(role, str) and role in self._bindings


class ProviderPool:
    """Resolves bindings, accounts and providers, and performs the call.

    The pool is the only component that talks to both the account store and a
    provider. It holds no key and no provider instance between calls: every call
    rebuilds the provider from the account that is usable *at that moment*.
    """

    def __init__(
        self,
        accounts: AccountStore,
        bindings: BindingRegistry,
        *,
        governor: TokenGovernor | None = None,
        builder: ProviderBuilder | None = None,
    ) -> None:
        self._accounts = accounts
        self._bindings = bindings
        self._governor = governor
        self._builder = builder if builder is not None else default_builder
        self._cursors: dict[str, int] = {}

    # -- resolution --------------------------------------------------------
    def resolve_binding(self, role: str | BindingRole) -> AgentBinding:
        """Resolve the binding of a role right now."""
        return self._bindings.resolve(role)

    def get_provider(self, binding: AgentBinding) -> LLMProvider:
        """Build a provider for the first usable account of a binding.

        Raises:
            NoAccountAvailable: every account of the binding is missing or disabled.
            ProviderUnsupported: the account's provider has no adapter (yet).
        """
        accounts = self._ordered_accounts(binding)
        if not accounts:
            raise NoAccountAvailable(
                f"no usable account for role {binding.role.value!r}",
                provider=binding.role.value,
            )
        account = accounts[0]
        provider = self._build(account)
        return provider

    # -- calling -----------------------------------------------------------
    async def call(
        self,
        request: LLMRequest,
        *,
        role: str | BindingRole,
        binding: AgentBinding | None = None,
        run_state: object = None,
    ) -> LLMResponse:
        """Perform one provider call, with failover and accounting.

        Args:
            request: the call to make.
            role: the binding role; it is also the key the mock scripts replies by.
            binding: the binding to use; resolved from the registry when omitted.
            run_state: optional run state, handed to the governor.

        Returns:
            The provider's response, already charged to the governor and to the
            account's own budget.

        Raises:
            NoAccountAvailable: the role has no binding, or no account of the
                binding is usable.
            BudgetExceeded: the call does not fit into a cap, or the account's own
                monthly budget is exhausted.
            ProviderError: every candidate account failed.
        """
        resolved = binding if binding is not None else self.resolve_binding(role)
        if self._governor is not None:
            self._governor.precheck(request, resolved, run_state)

        accounts = self._ordered_accounts(resolved)
        if not accounts:
            raise NoAccountAvailable(
                f"no usable account for role {resolved.role.value!r}",
                provider=resolved.role.value,
            )

        estimated = prompt_tokens(request) + request.max_output_tokens
        last_error: ProviderError | None = None
        budget_error: BudgetExceeded | None = None

        for account in accounts:
            exhausted = self._account_budget_error(account, estimated)
            if exhausted is not None:
                # This account is out of its own budget; another account of the
                # binding may still be able to pay for the call.
                budget_error = exhausted
                continue

            try:
                provider = self._build(account)
            except ProviderError as exc:
                # An unusable account (disabled, unsupported adapter) is not a
                # failure of the call: try the next one.
                last_error = exc
                continue

            try:
                response = await provider.generate(
                    request,
                    model=resolved.model,
                    role=resolved.role.value,
                    account_id=account.id,
                )
            except ProviderError as exc:
                last_error = exc
                continue

            self._charge(response.usage, account)
            return response

        if last_error is not None:
            raise last_error
        if budget_error is not None:
            raise budget_error
        raise NoAccountAvailable(  # pragma: no cover - the loop above always returns or raises
            f"no usable account for role {resolved.role.value!r}",
            provider=resolved.role.value,
        )

    # -- internals ---------------------------------------------------------
    def _ordered_accounts(self, binding: AgentBinding) -> list[Account]:
        """The binding's accounts that exist and are usable, in strategy order.

        An account that was deleted after the binding was written is skipped -
        a stale binding must not resurrect a deleted credential.
        """
        accounts: list[Account] = []
        for account_id in binding.account_ids:
            account = self._accounts.get(account_id)
            if account is None or not account.is_usable:
                continue
            accounts.append(account)

        if binding.strategy is BindingStrategy.DEDICATED:
            return accounts[:1]

        if binding.strategy is BindingStrategy.ROUND_ROBIN:
            if not accounts:
                return []
            key = binding.role.value
            start = self._cursors.get(key, 0) % len(accounts)
            self._cursors[key] = (start + 1) % len(accounts)
            return accounts[start:] + accounts[:start]

        return accounts

    def _build(self, account: Account) -> LLMProvider:
        """Build the provider for an account, decrypting its key when needed.

        The key is decrypted *here*, at call time, and handed to the builder - the
        only component allowed to see it. The mock ignores key material, and the
        live adapters do not exist yet, so the shipped builder refuses them
        (:func:`app.providers.factory.build_provider`).
        """
        key = (
            None if account.provider is AccountProvider.MOCK else self._accounts.reveal(account.id)
        )
        return self._builder(account, key=key)

    def _account_budget_error(self, account: Account, estimated: int) -> BudgetExceeded | None:
        """The error when an account's own monthly budget cannot pay for a call.

        Returns ``None`` when the account can pay. The caller decides what to do
        with an exhausted account: a binding may well have another one.
        """
        remaining = self._accounts.remaining(account)
        if remaining is None or estimated <= remaining:
            return None
        return BudgetExceeded(
            "the account's own budget is exhausted",
            cap="account_monthly_budget",
            limit=account.monthly_budget.tokens,
            remaining=remaining,
            requested=estimated,
            account_id=account.id,
        )

    def _charge(self, usage: TokenUsage, account: Account) -> None:
        """Record the real usage of a call against the governor and the account."""
        if self._governor is not None:
            self._governor.charge(usage, account_id=account.id)
        self._accounts.record_usage(account.id, usage.total)
