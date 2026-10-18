"""OpenAICompatibleProvider - one adapter for every Chat Completions dialect.

Anything that speaks ``POST {base_url}/chat/completions`` works: OpenAI, OpenRouter,
Together, Groq, Ollama, vLLM... The account supplies ``base_url`` and the key; this
adapter never hardcodes a vendor. That breadth is also its hazard - dialects differ
in optional fields - so uncertain request/response details are marked ``# VERIFY``
and the adapter degrades instead of guessing:

* ``json_schema`` goes out as ``response_format`` (OpenAI shape). A 400 that
  mentions ``response_format`` is retried once *without* it, with the schema
  folded into the system instruction - older services can still answer;
* prompt-cache hints (``SystemBlock.cache``) have no standard wire meaning here;
  prefix caching is the server's decision. ``cached`` usage is honoured when the
  server reports it;
* usage is normalised to the non-overlapping ``TokenUsage``: OpenAI's
  ``prompt_tokens`` *includes* cached tokens, so ``in_`` = prompt - cached.

Same retry policy as the Anthropic adapter: 429/5xx/timeout/transport only, full
jitter, honouring ``retry-after``.
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
    ProviderUnsupported,
)

__all__ = ["DEFAULT_TIMEOUT_S", "OpenAICompatibleProvider"]

logger = get_logger(__name__)

#: Placeholder model for ``model=None`` calls; bindings carry the real id.
DEFAULT_MODEL: str = "gpt-4o-mini"  # VERIFY: per-service default
DEFAULT_TIMEOUT_S: float = 60.0
_RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
_RESPONSE_FORMAT_MARKERS = ("response_format", "json_schema", "structured output", "not supported")


class OpenAICompatibleProvider:
    """LLMProvider adapter for OpenAI-style Chat Completions endpoints."""

    name: ClassVar[str] = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        backoff_cap_s: float = 8.0,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ProviderUnsupported(
                "openai_compatible accounts need an absolute http(s) base_url",
                provider=self.name,
            )
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
                    "authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- request shaping --------------------------------------------------
    def _body(
        self,
        request: LLMRequest,
        model: str,
        *,
        with_response_format: bool = True,
    ) -> dict[str, Any]:
        system_text = "\n\n".join(block.text for block in request.system_blocks)
        messages: list[dict[str, str]] = []
        if request.json_schema is not None and not with_response_format:
            schema_note = (
                "Reply with a JSON document only (no prose, no markdown fences) that "
                f"conforms to this JSON Schema: {json.dumps(request.json_schema)}"
            )
            system_text = f"{system_text}\n\n{schema_note}" if system_text else schema_note
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for message in request.messages:
            messages.append({"role": message.role.value, "content": message.text})

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.json_schema is not None and with_response_format:
            # VERIFY: json_schema response_format is OpenAI's shape; services that
            # do not implement it are handled by the fallback retry in generate().
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "emit", "strict": True, "schema": request.json_schema},
            }
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
        started = time.monotonic()
        body = self._body(request, resolved_model)
        try:
            payload = await self._request_json("POST", "/chat/completions", body)
        except ProviderBadResponse as exc:
            if request.json_schema is None or not _wants_response_format_fallback(exc):
                raise
            logger.info(
                "service rejected response_format; retrying with schema in the prompt (%s)",
                exc.code,
            )
            body = self._body(request, resolved_model, with_response_format=False)
            payload = await self._request_json("POST", "/chat/completions", body)
        latency_ms = int((time.monotonic() - started) * 1000)
        text = self._extract_text(payload)
        usage = self._map_usage(payload.get("usage") or {})
        return LLMResponse(text=text, usage=usage, model=resolved_model, latency_ms=latency_ms)

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """No portable token-count endpoint exists across services; say unknown."""
        return None

    # -- response parsing -------------------------------------------------
    def _extract_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderBadResponse("response has no choices", provider=self.name)
        first = choices[0]
        if not isinstance(first, dict):
            raise ProviderBadResponse("choice is not an object", provider=self.name)
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ProviderBadResponse("choice carries no text content", provider=self.name)
        return content

    @staticmethod
    def _map_usage(usage: dict[str, Any]) -> TokenUsage:
        """OpenAI-style usage: prompt_tokens includes cached_tokens (# VERIFY
        ``prompt_tokens_details.cached_tokens`` is the widest convention)."""

        def _int(key: str) -> int:
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        prompt = _int("prompt_tokens")
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, (int, float)):
                cached = int(value)
        cached = min(cached, prompt)
        return TokenUsage(
            **{"in": prompt - cached, "out": _int("completion_tokens"), "cached": cached}
        )

    # -- HTTP + retries ---------------------------------------------------
    def _backoff_s(self, attempt: int, retry_after: float | None) -> float:
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
                error = ProviderTimeout("chat-completions request timed out", provider=self.name)
            except httpx.TransportError as exc:
                error = ProviderUnavailable(f"transport error: {exc}", provider=self.name)
            else:
                error = self._error_for(response)
                if error is None:
                    return self._parse_json(response)

            if not error.retryable or attempt >= budget:
                raise error
            retry_after = error.retry_after_s
            delay = self._backoff_s(attempt, retry_after)
            logger.warning(
                "openai-compatible %s attempt %d failed (%s); retrying in %.2fs",
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
                "service returned malformed JSON", provider=self.name
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderBadResponse("service returned a non-object body", provider=self.name)
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
        if status in _RETRIABLE_STATUSES or status >= 500:
            return ProviderUnavailable(message, provider=self.name, status_code=status)
        return ProviderBadResponse(message, provider=self.name, status_code=status)

    def _error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"]
            elif isinstance(error, str):
                detail = error
        raw = (
            f"chat-completions API error {response.status_code}: {detail or response.reason_phrase}"
        )
        return redact(raw)


def _wants_response_format_fallback(exc: ProviderBadResponse) -> bool:
    """True when a 400 is about response_format, not about the call itself."""
    if exc.status_code not in (400, 404, 422):
        return False
    lowered = exc.message.lower()
    return any(marker in lowered for marker in _RESPONSE_FORMAT_MARKERS)


def _header_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
