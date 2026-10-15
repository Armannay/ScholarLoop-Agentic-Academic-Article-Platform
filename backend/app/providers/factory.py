"""Provider construction from an :class:`~app.contracts.account.Account`.

This is the single place where a stored account turns into something that can spend
tokens, and it is deliberately the *only* such place. Accounts are resolved at call
time (users add, disable and delete them mid-run), so nothing above this module holds
a provider instance across a call boundary.

Only the mock is implemented in this step. The live branches raise
:class:`ProviderUnsupported` and carry ``# VERIFY`` markers instead of guessed
endpoints: inventing a vendor detail here would bake an unverified assumption into
every later step.
"""

from __future__ import annotations

from app.contracts.account import Account
from app.contracts.enums import AccountProvider
from app.providers.errors import NoAccountAvailable, ProviderUnsupported
from app.providers.mock import MockProvider

__all__ = ["SUPPORTED_PROVIDERS", "build_provider", "provider_supported"]

#: Providers with a working adapter today.
SUPPORTED_PROVIDERS: frozenset[AccountProvider] = frozenset({AccountProvider.MOCK})


def provider_supported(provider: AccountProvider) -> bool:
    """True when an adapter exists for ``provider``."""
    return provider in SUPPORTED_PROVIDERS


def build_provider(account: Account) -> MockProvider:
    """Build the provider for ``account``, or explain why it cannot be built.

    Args:
        account: the resolved account (never a key: the account carries a ciphertext
            that only the account store can open).

    Raises:
        NoAccountAvailable: the account is disabled, or its key cannot be resolved.
        ProviderUnsupported: the provider has no adapter in this step.
    """
    if not account.is_usable:
        raise NoAccountAvailable(
            f"account {account.id} is {account.status.value}", provider=account.provider.value
        )

    if account.provider is AccountProvider.MOCK:
        # The mock ignores key material entirely: there is nothing to decrypt.
        return MockProvider()

    if account.provider is AccountProvider.ANTHROPIC:
        # VERIFY: endpoint, headers, json-schema support mode and token-usage field
        # names before implementing. Isolate every vendor detail in the adapter and
        # keep this function pure glue.
        raise ProviderUnsupported(
            "the anthropic adapter is not implemented yet",
            provider=account.provider.value,
        )

    if account.provider is AccountProvider.OPENAI_COMPATIBLE:
        # VERIFY: whether the configured base_url speaks the Chat Completions or the
        # Responses shape, and how it reports cached prompt tokens.
        raise ProviderUnsupported(
            "the openai_compatible adapter is not implemented yet",
            provider=account.provider.value,
            base_url=account.base_url,
        )

    raise ProviderUnsupported(  # pragma: no cover - every enum member is handled above
        f"no adapter for provider {account.provider.value}", provider=account.provider.value
    )
