"""Provider errors - the only failures an adapter is allowed to raise.

The hierarchy exists so the orchestrator can decide *retry vs. fail vs. ask the
user* without knowing which vendor it is talking to. ``retryable`` is a class-level
fact, not a guess made at the call site.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.errors import ScholarLoopError

__all__ = [
    "NoAccountAvailable",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderQuotaExceeded",
    "ProviderRateLimited",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ProviderUnsupported",
]


class ProviderError(ScholarLoopError):
    """Base class for every provider-boundary failure."""

    code: ClassVar[str] = "provider_error"
    http_status: ClassVar[int] = 502
    #: True when the identical call may succeed later without a change by us.
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        retry_after_s: float | None = None,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            status_code=status_code,
            retry_after_s=retry_after_s,
            **context,
        )
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.retry_after_s = retry_after_s


class ProviderAuthError(ProviderError):
    """401/403: the key is missing, revoked or lacks scope. Never retried blindly."""

    code = "provider_auth_error"
    http_status = 401
    retryable = False


class ProviderRateLimited(ProviderError):
    """429: too many requests or out of quota for the window."""

    code = "provider_rate_limited"
    http_status = 429
    retryable = True


class ProviderQuotaExceeded(ProviderError):
    """402/insufficient_quota: the account is out of money, not out of patience."""

    code = "provider_quota_exceeded"
    http_status = 402
    retryable = False


class ProviderTimeout(ProviderError):
    """The call exceeded its deadline."""

    code = "provider_timeout"
    http_status = 504
    retryable = True


class ProviderUnavailable(ProviderError):
    """5xx or a transport error: the vendor is unreachable."""

    code = "provider_unavailable"
    http_status = 503
    retryable = True


class ProviderBadResponse(ProviderError):
    """The response was not usable: malformed JSON, wrong schema, empty body.

    Not retryable by default - a model that answered garbage will usually answer
    garbage again - but the orchestrator may spend a repair retry on it.
    """

    code = "provider_bad_response"
    http_status = 502
    retryable = False


class ProviderUnsupported(ProviderError):
    """The adapter does not implement the requested feature (e.g. json_schema)."""

    code = "provider_unsupported"
    http_status = 501
    retryable = False


class NoAccountAvailable(ProviderError):
    """No usable account for the role: deleted, disabled, or over its own budget.

    This is a *user* condition (accounts are resolved at call time), which is why it
    maps to 409 and the loop turns it into ``needs_user_input``.
    """

    code = "no_account_available"
    http_status = 409
    retryable = False
