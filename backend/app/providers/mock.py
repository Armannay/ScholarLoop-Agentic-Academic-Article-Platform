"""MockProvider - the only provider the test-suite is allowed to touch (HARD RULE 5).

Features, all deterministic:

* **Scripted replies keyed by ``(role, call index)``.** Call index counts calls made
  *by that role*, starting at 0. An unscripted call returns a well-formed stub naming
  the role and index instead of failing, so a test that forgets to script a call still
  gets a usable, obviously-synthetic answer.
* **Failure injection** for the three failures the loop must survive: ``timeout``,
  ``rate_limit`` and ``malformed_json`` (returned as text, so the *caller* has to
  notice - exactly like a real model that ignores a JSON schema). Failures are checked
  before replies, so ``fail_next()`` plus a reply scripted at the following index is the
  canonical "retry succeeds" scenario.
* **Deterministic token accounting** with the shared ``chars / 4`` convention.
* **No network, no keys**: the class has no HTTP client and no credential field, so a
  live call is not merely avoided - it is impossible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field

from app.contracts.binding import AgentBinding  # noqa: F401  (typing reference in docs)
from app.contracts.common import Contract
from app.contracts.enums import BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.providers.base import CHARS_PER_TOKEN, cached_prompt_tokens, prompt_tokens
from app.providers.errors import (
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnsupported,
)

__all__ = [
    "FAILURE_MODES",
    "MALFORMED_JSON_TEXT",
    "FailureMode",
    "MockCall",
    "MockFailure",
    "MockProvider",
    "ProviderError",
    "Script",
    "reply_key",
]

DEFAULT_MODEL: str = "mock-standard-1"
#: A reply that starts like JSON and then is not - the caller must reject it.
MALFORMED_JSON_TEXT: str = '{"reply": "unterminated string'


class FailureMode(StrEnum):
    """The three injected failures."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    MALFORMED_JSON = "malformed_json"


FAILURE_MODES: tuple[str, ...] = tuple(mode.value for mode in FailureMode)


def _role_name(role: BindingRole | str) -> str:
    """Normalise a role to its wire string."""
    return role.value if isinstance(role, BindingRole) else str(role)


def reply_key(role: BindingRole | str, call_index: int) -> tuple[str, int]:
    """Canonical script key: ``(role, per-role call index)``."""
    return (_role_name(role), call_index)


class MockFailure(Contract):
    """A failure to inject for one role, starting at a call index.

    Matching rule: the failure applies from ``call_index`` onwards and fires at most
    ``times`` times. So ``times=1`` is the *intermittent* case (fail once, then
    succeed - the "retry works" scenario), while ``times=5`` is an outage that keeps
    failing and eventually trips the ``error_streak`` guard.
    """

    role: str | None = Field(
        default=None, description="None = any role (still keyed to its call index)."
    )
    call_index: int = Field(ge=0)
    mode: str = Field(description="timeout | rate_limit | malformed_json")
    times: int = Field(default=1, ge=1, le=100)
    retry_after_s: float = Field(default=1.0, ge=0.0, le=600.0)

    def model_post_init(self, __context: Any) -> None:
        if self.mode not in FAILURE_MODES:
            raise ValueError(f"mode must be one of {FAILURE_MODES}, got {self.mode!r}")


class MockCall(Contract):
    """Record of one call, for assertions. Holds the prompt *size*, never the prompt."""

    index: int = Field(ge=0, description="Per-role call index.")
    global_index: int = Field(ge=0)
    role: str
    model: str
    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    served_scripted: bool
    failed_with: str | None = None


class Script:
    """Fluent builder for a mock's replies, so tests read like a screenplay."""

    def __init__(self) -> None:
        self._replies: dict[tuple[str, int], str] = {}
        self._failures: list[MockFailure] = []

    def reply(self, role: BindingRole | str, call_index: int, text: str) -> Script:
        """Answer ``call_index`` of ``role`` with ``text``."""
        self._replies[reply_key(role, call_index)] = text
        return self

    def json_reply(
        self, role: BindingRole | str, call_index: int, payload: Mapping[str, Any]
    ) -> Script:
        """Answer with a serialised JSON document (stable key order, no spaces)."""
        return self.reply(
            role, call_index, json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        )

    def fail(
        self,
        role: BindingRole | str | None,
        call_index: int,
        mode: FailureMode | str,
        *,
        times: int = 1,
        retry_after_s: float = 1.0,
    ) -> Script:
        """Inject a failure at ``(role, call_index)``; ``role=None`` matches any role."""
        self._failures.append(
            MockFailure(
                role=None if role is None else _role_name(role),
                call_index=call_index,
                mode=str(mode),
                times=times,
                retry_after_s=retry_after_s,
            )
        )
        return self

    @property
    def replies(self) -> dict[tuple[str, int], str]:
        """The scripted reply map."""
        return dict(self._replies)

    @property
    def failures(self) -> list[MockFailure]:
        """The configured failures."""
        return list(self._failures)


class MockProvider:
    """A provider whose entire behaviour is decided by its script."""

    name: ClassVar[str] = "mock"

    def __init__(
        self,
        script: Script | None = None,
        *,
        default_model: str = DEFAULT_MODEL,
        latency_ms: int = 7,
        supports_json_schema: bool = True,
    ) -> None:
        self._replies: dict[tuple[str, int], str] = dict(script.replies) if script else {}
        self._failures: list[MockFailure] = list(script.failures) if script else []
        self._consumed_failures: dict[int, int] = {}
        self._role_calls: dict[str, int] = {}
        self._calls: list[MockCall] = []
        self._default_model = default_model
        self._latency_ms = latency_ms
        self._supports_json_schema = supports_json_schema

    # -- scripting ---------------------------------------------------------
    def script_reply(self, role: BindingRole | str, call_index: int, text: str) -> MockProvider:
        """Register a reply after construction."""
        self._replies[reply_key(role, call_index)] = text
        return self

    def script_json(
        self, role: BindingRole | str, call_index: int, payload: Mapping[str, Any]
    ) -> MockProvider:
        """Register a JSON reply after construction."""
        return self.script_reply(
            role, call_index, json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        )

    def fail_next(
        self,
        mode: FailureMode | str,
        *,
        role: BindingRole | str | None = None,
        times: int = 1,
        retry_after_s: float = 1.0,
    ) -> MockProvider:
        """Inject a failure at the *next* call for ``role`` (the common case in tests)."""
        key = "" if role is None else _role_name(role)
        next_index = len(self._calls) if role is None else self._role_calls.get(key, 0)
        self._failures.append(
            MockFailure(
                role=key or None,
                call_index=next_index,
                mode=str(mode),
                times=times,
                retry_after_s=retry_after_s,
            )
        )
        return self

    # -- inspection --------------------------------------------------------
    @property
    def calls(self) -> list[MockCall]:
        """Every call made so far, in order."""
        return list(self._calls)

    @property
    def call_count(self) -> int:
        """Total calls attempted, including the ones that failed.

        A failed attempt is part of the run: ``error_streak`` counts attempts, and a
        timeout still costs wall-clock time even when it costs no tokens.
        """
        return len(self._calls)

    @property
    def failed_calls(self) -> list[MockCall]:
        """Calls that raised a ProviderError."""
        return [call for call in self._calls if call.failed_with is not None]

    def calls_for(self, role: BindingRole | str) -> list[MockCall]:
        """Calls made by one role."""
        return [call for call in self._calls if call.role == _role_name(role)]

    def total_tokens(self) -> TokenUsage:
        """Summed usage across every call - the number the governor must match."""
        return TokenUsage(
            **{
                "in": sum(call.prompt_tokens for call in self._calls),
                "out": sum(call.output_tokens for call in self._calls),
                "cached": sum(call.cached_tokens for call in self._calls),
            }
        )

    def reset(self) -> None:
        """Forget every call (script and failure configuration are kept)."""
        self._role_calls.clear()
        self._calls.clear()
        self._consumed_failures.clear()

    # -- LLMProvider -------------------------------------------------------
    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        """Return the scripted reply (or the injected failure) for this call."""
        role_name = role or "unknown"
        index = self._role_calls.get(role_name, 0)
        self._role_calls[role_name] = index + 1
        resolved_model = model or self._default_model
        latency = self._latency_ms + index

        failure = self._match_failure(role_name, index)
        if failure is not None:
            # Record the attempt before raising: the call log describes the run, and a
            # failed attempt costs latency even when it costs no tokens.
            self._record(
                role=role_name,
                index=index,
                model=resolved_model,
                prompt=0,
                output=0,
                cached=0,
                latency=latency,
                served_scripted=False,
                failed_with=failure.mode,
            )
            self._raise_for(failure)

        if request.json_schema is not None and not self._supports_json_schema:
            raise ProviderUnsupported(
                "this mock is configured without JSON-schema support",
                provider=self.name,
                model=model,
            )

        scripted = self._replies.get(reply_key(role_name, index))
        served_scripted = scripted is not None
        text = scripted if scripted is not None else self._stub_reply(role_name, index, request)

        cached = cached_prompt_tokens(request)
        prompt = prompt_tokens(request)
        usage = TokenUsage(
            **{
                "in": max(0, prompt - cached),
                "out": _estimate_output(text),
                "cached": cached,
            }
        )
        self._record(
            role=role_name,
            index=index,
            model=resolved_model,
            prompt=usage.in_,
            output=usage.out,
            cached=usage.cached,
            latency=latency,
            served_scripted=served_scripted,
            failed_with=None,
        )
        _ = account_id  # billing identity is the caller's concern, not the mock's
        return LLMResponse(
            text=text,
            usage=usage,
            model=resolved_model,
            latency_ms=latency,
        )

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Exact for the shared ``chars / 4`` convention, so never ``None``."""
        return prompt_tokens(request)

    async def aclose(self) -> None:
        """No transport to close; provided so callers can call it unconditionally."""
        return None

    # -- internals ---------------------------------------------------------
    def _match_failure(self, role: str, index: int) -> MockFailure | None:
        """The failure to inject for this call, consuming one unit of its budget."""
        for position, failure in enumerate(self._failures):
            if failure.role is not None and failure.role != role:
                continue
            if index < failure.call_index:
                continue
            consumed = self._consumed_failures.get(position, 0)
            if consumed >= failure.times:
                continue
            self._consumed_failures[position] = consumed + 1
            return failure
        return None

    def _record(
        self,
        *,
        role: str,
        index: int,
        model: str,
        prompt: int,
        output: int,
        cached: int,
        latency: int,
        served_scripted: bool,
        failed_with: str | None,
    ) -> None:
        """Append one call record."""
        self._calls.append(
            MockCall(
                index=index,
                global_index=len(self._calls),
                role=role,
                model=model,
                prompt_tokens=prompt,
                output_tokens=output,
                cached_tokens=cached,
                latency_ms=latency,
                served_scripted=served_scripted,
                failed_with=failed_with,
            )
        )

    def _raise_for(self, failure: MockFailure) -> None:
        """Translate an injected failure into the matching ProviderError."""
        if failure.mode == FailureMode.TIMEOUT:
            raise ProviderTimeout("injected timeout", provider=self.name)
        if failure.mode == FailureMode.RATE_LIMIT:
            raise ProviderRateLimited(
                "injected rate limit",
                provider=self.name,
                status_code=429,
                retry_after_s=failure.retry_after_s,
            )
        if failure.mode == FailureMode.MALFORMED_JSON:
            raise ProviderBadResponse(
                "injected malformed JSON response",
                provider=self.name,
                status_code=200,
                body_preview=MALFORMED_JSON_TEXT,
            )
        raise AssertionError(f"unhandled failure mode {failure.mode!r}")  # pragma: no cover

    @staticmethod
    def _stub_reply(role: str, index: int, request: LLMRequest) -> str:
        """A deterministic, obviously-synthetic answer for an unscripted call."""
        payload = {
            "mock": True,
            "role": role,
            "call_index": index,
            "schema_requested": request.json_schema is not None,
            "note": "unscripted MockProvider call",
        }
        if request.json_schema is not None:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"[mock:{role}:{index}] unscripted reply"


def _estimate_output(text: str) -> int:
    """``ceil(chars / 4)`` for a reply - the same convention as the request side."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
