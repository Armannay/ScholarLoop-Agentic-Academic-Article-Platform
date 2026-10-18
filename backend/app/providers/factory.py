"""Provider construction from an :class:`~app.contracts.account.Account`.

This is the single place where a stored account turns into something that can spend
tokens, and it is deliberately the *only* such place. Accounts are resolved at call
time (users add, disable and delete them mid-run), so nothing above this module holds
a provider instance across a call boundary.

The account store opens the envelope ciphertext and hands the plaintext key here;
this module never decrypts and never logs it. ``# VERIFY`` markers on vendor
details live with the adapters, not here - this function is pure glue.
"""

from __future__ import annotations

from app.contracts.account import Account
from app.contracts.enums import AccountProvider
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.errors import NoAccountAvailable, ProviderUnsupported
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider

__all__ = ["SUPPORTED_PROVIDERS", "build_provider", "provider_supported"]

#: Providers with a working adapter today.
SUPPORTED_PROVIDERS: frozenset[AccountProvider] = frozenset(
    {AccountProvider.MOCK, AccountProvider.ANTHROPIC, AccountProvider.OPENAI_COMPATIBLE}
)


def provider_supported(provider: AccountProvider) -> bool:
    """True when an adapter exists for ``provider``."""
    return provider in SUPPORTED_PROVIDERS


def build_provider(account: Account, *, api_key: str | None = None) -> LLMProvider:
    """Build the provider for ``account``, or explain why it cannot be built.

    Args:
        account: the resolved account (never a key: the account carries a ciphertext
            that only the account store can open).
        api_key: the decrypted key, supplied by the account store for one call.
            The mock ignores it entirely; live adapters cannot be built without it.

    Raises:
        NoAccountAvailable: the account is disabled, or its key cannot be resolved.
        ProviderUnsupported: the provider has no adapter.
    """
    if not account.is_usable:
        raise NoAccountAvailable(
            f"account {account.id} is {account.status.value}", provider=account.provider.value
        )

    if account.provider is AccountProvider.MOCK:
        # The mock ignores key material entirely: there is nothing to decrypt.
        return MockProvider()

    if account.provider is AccountProvider.ANTHROPIC:
        if not api_key:
            raise NoAccountAvailable(
                f"account {account.id}: key could not be resolved from the account store",
                provider=account.provider.value,
            )
        return AnthropicProvider(api_key)

    if account.provider is AccountProvider.OPENAI_COMPATIBLE:
        if not api_key:
            raise NoAccountAvailable(
                f"account {account.id}: key could not be resolved from the account store",
                provider=account.provider.value,
            )
        if not account.base_url:
            raise ProviderUnsupported(
                f"account {account.id}: openai_compatible needs a base_url",
                provider=account.provider.value,
            )
        return OpenAICompatibleProvider(api_key, base_url=account.base_url)

    raise ProviderUnsupported(  # pragma: no cover - every enum member is handled above
        f"no adapter for provider {account.provider.value}", provider=account.provider.value
    )
