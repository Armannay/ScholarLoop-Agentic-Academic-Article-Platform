"""Provider adapters. One Protocol (:class:`LLMProvider`), one mock, one error family.

Live adapters (``AnthropicProvider``, ``OpenAICompatibleProvider``) sit behind the
same interface as the mock; vendor details stay inside each adapter and uncertain
request/response fields are marked ``# VERIFY``. The pool
(:mod:`app.providers.pool`) is the only caller in production code: it resolves
accounts at call time and never exposes a key to the run loop.
"""

from __future__ import annotations

from app.providers.anthropic import ANTHROPIC_VERSION_HEADER, AnthropicProvider
from app.providers.base import (
    CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD_TOKENS,
    LLMProvider,
    ProviderRouting,
    estimate_tokens,
    prompt_tokens,
)
from app.providers.errors import (
    NoAccountAvailable,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.factory import SUPPORTED_PROVIDERS, build_provider, provider_supported
from app.providers.mock import (
    DEFAULT_MODEL,
    FAILURE_MODES,
    FailureMode,
    MockCall,
    MockFailure,
    MockProvider,
    Script,
    reply_key,
)
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.pool import (
    BreakerState,
    CircuitBreaker,
    ProviderPool,
    UsageRecord,
)

__all__ = [
    "ANTHROPIC_VERSION_HEADER",
    "CHARS_PER_TOKEN",
    "DEFAULT_MODEL",
    "FAILURE_MODES",
    "MESSAGE_OVERHEAD_TOKENS",
    "SUPPORTED_PROVIDERS",
    "AnthropicProvider",
    "BreakerState",
    "CircuitBreaker",
    "FailureMode",
    "LLMProvider",
    "MockCall",
    "MockFailure",
    "MockProvider",
    "NoAccountAvailable",
    "OpenAICompatibleProvider",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderPool",
    "ProviderQuotaExceeded",
    "ProviderRateLimited",
    "ProviderRouting",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ProviderUnsupported",
    "Script",
    "UsageRecord",
    "build_provider",
    "estimate_tokens",
    "prompt_tokens",
    "provider_supported",
    "reply_key",
]
