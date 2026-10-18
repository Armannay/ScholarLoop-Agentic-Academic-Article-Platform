"""AnthropicProvider - the Messages API behind :class:`LLMProvider`.

Built for the Planner (and any role bound to an ``anthropic`` account) from a
**Claude Console API key** (console.anthropic.com). A Claude Pro/Max subscription
login is not an API credential: its OAuth session tokens are rejected at the
account store and would fail here regardless.

Request mapping:

* ``system_blocks`` -> ``system`` as a list of text blocks; blocks with
  ``cache=true`` carry ``cache_control: {"type": "ephemeral"}`` so the stable
  prefix (rules, digest) is served from the prompt cache;
* ``messages`` -> alternating ``user``/``assistant`` turns with text content;
* ``json_schema`` -> a single forced tool (``tool_choice``) whose ``input`` is the
  structured answer (the documented way to get schema-shaped JSON; # VERIFY
  against current structured-output docs before changing);
* usage -> :class:`~app.contracts.ledger.TokenUsage`, normalised to non-overlap:
  ``in_`` = uncached prompt + cache *creation* (both billed at full rate),
  ``cached`` = cache *reads* (billed cheaper), ``out`` = completion.

Retries: only 429/5xx/timeout/transport, with full jitter, honouring
``retry-after``. Auth and quota errors are never retried. Every vendor string that
enters an exception or log passes through :func:`app.core.redaction.redact` first.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, ClassVar

import httpx

from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.core.logging import get_logger
from app.core.redaction import redact
from app.providers.errors import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

__all__ = ["ANTHROPIC_VERSION_HEADER", "DEFAULT_TIMEOUT_S", "AnthropicProvider"]

logger = get_logger(__name__)

#: Messages API version pin. VERIFY: bump deliberately, never silently.
ANTHROPIC_VERSION_HEADER: str = "2023-06-01"
DEFAULT_BASE_URL: str = "https://api.anthropic.com"
#: Default model used when a call passes ``model=None`` (bindings carry the real id).
DEFAULT_MODEL: str = "claude-sonnet-4-5"  # VERIFY: current model alias
DEFAULT_TIMEOUT_S: float = 60.0
_TOOL_NAME = "emit"

_RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})


class AnthropicProvider:
    """LLMProvider adapter for Anthropic's Messages API (``/v1/messages``)."""

    name: ClassVar[str] = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        backoff_cap_s: float = 8.0,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
        self._max_retries = max(0, max_retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 (backoff jitter)
        self._client = client
        self._owns_client = client is None

    # -- transport --------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION_HEADER,
                    "content-type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- request shaping --------------------------------------------------
    def _body(self, request: LLMRequest, model: str) -> dict[str, Any]:
        system: list[dict[str, Any]] = []
        for block in request.system_blocks:
            shaped: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache:
                # VERIFY: cache_control is the documented prompt-caching marker.
                shaped["cache_control"] = {"type": "ephemeral"}
            system.append(shaped)

        messages = [
            {"role": message.role.value, "content": [{"type": "text", "text": message.text}]}
            for message in request.messages
        ]

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if request.json_schema is not None:
            # Forced tool use: the model must return input matching the schema.
            body["tools"] = [
                {
                    "name": _TOOL_NAME,
                    "description": "Emit the structured answer.",
                    "input_schema": request.json_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
        return body

    def _count_body(self, request: LLMRequest, model: str) -> dict[str, Any]:
        body = self._body(request, model)
        body.pop("tools", None)
        body.pop("tool_choice", None)
        return body

    # -- LLMProvider ------------------------------------------------------
    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        resolved_model = model or self._model
        body = self._body(request, resolved_model)
        started = time.monotonic()
        payload = await self._request_json("POST", "/v1/messages", body)
        latency_ms = int((time.monotonic() - started) * 1000)
        text = self._extract_text(payload)
        usage = self._map_usage(payload.get("usage") or {})
        reported_model = payload.get("model")
        return LLMResponse(
            text=text,
            usage=usage,
            model=str(reported_model) if isinstance(reported_model, str) else resolved_model,
            latency_ms=latency_ms,
        )

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Best-effort prompt size via the count_tokens endpoint, else None."""
        body = self._count_body(request, self._model)
        try:
            payload = await self._request_json("POST", "/v1/messages/count_tokens", body, retries=1)
        except ProviderError:
            return None
        value = payload.get("input_tokens")
        return int(value) if isinstance(value, int) else None

    # -- response parsing -------------------------------------------------
    def _extract_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if not isinstance(content, list):
            raise ProviderBadResponse("response has no content list", provider=self.name)
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("name") == _TOOL_NAME:
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    raise ProviderBadResponse(
                        "tool_use block has no object input", provider=self.name
                    )
                texts.append(json.dumps(tool_input, sort_keys=True))
        if not texts:
            raise ProviderBadResponse("response carried no text or tool input", provider=self.name)
        return "\n".join(texts)

    @staticmethod
    def _map_usage(usage: dict[str, Any]) -> TokenUsage:
        """Normalise Anthropic's disjoint usage fields to non-overlapping TokenUsage.

        Anthropic reports ``input_tokens`` *excluding* cache reads and cache
        creation. ``cache_creation_input_tokens`` is prompt text written to the
        cache at full rate, so it lands in ``in_``; ``cache_read_input_tokens`` is
        the cheaper cached traffic and lands in ``cached``. VERIFY against the
        current usage docs if the vendor changes these semantics.
        """

        def _int(key: str) -> int:
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        input_tokens = _int("input_tokens")
        cache_creation = _int("cache_creation_input_tokens")
        cache_read = _int("cache_read_input_tokens")
        return TokenUsage(
            **{
                "in": input_tokens + cache_creation,
                "out": _int("output_tokens"),
                "cached": cache_read,
            }
        )

    # -- HTTP + retries ---------------------------------------------------
    def _backoff_s(self, attempt: int, retry_after: float | None) -> float:
        """Full-jitter exponential backoff, capped; retry-after wins when given."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self._backoff_cap_s)
        ceiling = min(self._backoff_cap_s, self._backoff_base_s * (2**attempt))
        return self._rng.uniform(0.0, ceiling)

    async def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        retries: int | None = None,
    ) -> dict[str, Any]:
        client = self._get_client()
        budget = self._max_retries if retries is None else min(retries, self._max_retries)
        attempt = 0
        while True:
            error: ProviderError | None = None
            try:
                response = await client.request(method, path, json=body)
            except httpx.TimeoutException:
                error = ProviderTimeout("anthropic request timed out", provider=self.name)
            except httpx.TransportError as exc:
                error = ProviderUnavailable(f"anthropic transport error: {exc}", provider=self.name)
            else:
                error = self._error_for(response)
                if error is None:
                    return self._parse_json(response)

            if not error.retryable or attempt >= budget:
                raise error
            retry_after = _retry_after_seconds(error)
            delay = self._backoff_s(attempt, retry_after)
            logger.warning(
                "anthropic %s attempt %d failed (%s); retrying in %.2fs",
                path,
                attempt + 1,
                error.code,
                delay,
            )
            await asyncio.sleep(delay)
            attempt += 1

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderBadResponse(
                "anthropic returned malformed JSON", provider=self.name
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderBadResponse("anthropic returned a non-object body", provider=self.name)
        return payload

    def _error_for(self, response: httpx.Response) -> ProviderError | None:
        if response.status_code == 200:
            return None
        message = self._error_message(response)
        status = response.status_code
        if status in (401, 403):
            return ProviderAuthError(message, provider=self.name, status_code=status)
        if status == 402:
            return ProviderQuotaExceeded(message, provider=self.name, status_code=status)
        if status == 429:
            return ProviderRateLimited(
                message,
                provider=self.name,
                status_code=status,
                retry_after_s=_header_retry_after(response),
            )
        if status == 408:
            return ProviderTimeout(message, provider=self.name, status_code=status)
        if status in _RETRIABLE_STATUSES:
            return ProviderUnavailable(message, provider=self.name, status_code=status)
        if status >= 500:
            return ProviderUnavailable(message, provider=self.name, status_code=status)
        # 4xx that is ours: malformed request, unknown model, bad tool schema.
        return ProviderBadResponse(message, provider=self.name, status_code=status)

    def _error_message(self, response: httpx.Response) -> str:
        """Vendor error text, redacted before it can reach a log or an exception."""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"]
            elif isinstance(payload.get("message"), str):
                detail = payload["message"]
        raw = f"anthropic API error {response.status_code}: {detail or response.reason_phrase}"
        return redact(raw)


def _header_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _retry_after_seconds(error: ProviderError) -> float | None:
    return error.retry_after_s
