"""Provider adapters. One Protocol (:class:`LLMProvider`), one mock, one error family.

Live adapters land in later steps; when they do, they must be isolated behind this
interface and marked with ``# VERIFY`` where a third-party detail is assumed.
"""

from __future__ import annotations

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

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_MODEL",
    "FAILURE_MODES",
    "MESSAGE_OVERHEAD_TOKENS",
    "FailureMode",
    "LLMProvider",
    "MockCall",
    "MockFailure",
    "MockProvider",
    "NoAccountAvailable",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderQuotaExceeded",
    "ProviderRateLimited",
    "ProviderRouting",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ProviderUnsupported",
    "Script",
    "estimate_tokens",
    "prompt_tokens",
    "reply_key",
]
