"""Live adapters behind LLMProvider, against respx-mocked HTTP (never a network).

Two adapters, one behaviour contract: cache hints map to the vendor's cache
marker, usage is normalised to the non-overlapping TokenUsage, 429/5xx get
jittered backoff, auth/quota do not, and every failure lands in the
ProviderError family. Vendor fields marked ``# VERIFY`` in the adapters are
pinned here in their *current* assumed shape so any drift fails loudly.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.contracts.llm import LLMRequest
from app.providers.anthropic import ANTHROPIC_VERSION_HEADER, AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.errors import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.openai_compatible import OpenAICompatibleProvider
from tests.test_accounts_crypto import api_key

pytestmark = pytest.mark.providers

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://openrouter.example/api/v1/chat/completions"


def make_request(*, cache: bool = True, schema: bool = False) -> LLMRequest:
    return LLMRequest(
        system_blocks=[{"text": "Stable system prefix.", "cache": cache}],
        messages=[{"role": "user", "text": "Say pong."}],
        max_output_tokens=64,
        json_schema={"type": "object"} if schema else None,
    )


def anthropic_body(content=None, usage=None, model="claude-sonnet-4-5") -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn",
        "usage": usage or {"input_tokens": 10, "output_tokens": 2},
    }


def fast_provider(provider: AnthropicProvider | OpenAICompatibleProvider) -> object:
    """Zero out backoff so retry tests stay instant and deterministic."""
    provider._backoff_base_s = 0.0  # type: ignore[attr-defined]
    provider._rng.seed(0)  # type: ignore[attr-defined]
    return provider


# --------------------------------------------------------------------------- anthropic
def test_anthropic_satisfies_the_protocol() -> None:
    assert isinstance(AnthropicProvider(api_key()), LLMProvider)
    assert AnthropicProvider.name == "anthropic"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_request_shape_maps_cache_control_and_headers() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=anthropic_body()))
    provider = AnthropicProvider(api_key(), model="claude-sonnet-4-5")
    response = await provider.generate(make_request(), role="planner")
    await provider.aclose()

    request = route.calls.last.request
    assert request.headers["x-api-key"] == api_key()
    assert request.headers["anthropic-version"] == ANTHROPIC_VERSION_HEADER
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 64
    # cache=true block carries cache_control; a cache=false block would not.
    assert body["system"] == [
        {
            "type": "text",
            "text": "Stable system prefix.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Say pong."}]}
    ]
    assert response.text == "pong"
    assert response.model == "claude-sonnet-4-5"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_uncached_system_block_has_no_marker() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=anthropic_body()))
    provider = AnthropicProvider(api_key())
    await provider.generate(make_request(cache=False))
    await provider.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body["system"] == [{"type": "text", "text": "Stable system prefix."}]


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_json_schema_becomes_a_forced_tool() -> None:
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_body(
                content=[{"type": "tool_use", "id": "t1", "name": "emit", "input": {"a": 1}}]
            ),
        )
    )
    provider = AnthropicProvider(api_key())
    response = await provider.generate(make_request(schema=True))
    await provider.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["tool_choice"] == {"type": "tool", "name": "emit"}
    assert json.loads(response.text) == {"a": 1}


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_usage_maps_cache_read_and_creation() -> None:
    # VERIFY pin: input_tokens excludes cache fields; cache creation is full-rate
    # prompt traffic, cache reads are the cheaper "cached" bucket.
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_body(
                usage={
                    "input_tokens": 50,
                    "output_tokens": 7,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 200,
                }
            ),
        )
    )
    provider = AnthropicProvider(api_key())
    response = await provider.generate(make_request())
    await provider.aclose()
    assert route.called
    assert response.usage.in_ == 80  # 50 uncached + 30 cache creation
    assert response.usage.cached == 200  # cache reads
    assert response.usage.out == 7
    assert response.usage.total == 287


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_maps_error_statuses_to_the_provider_family() -> None:
    cases = [
        (401, ProviderAuthError, False),
        (403, ProviderAuthError, False),
        (402, ProviderQuotaExceeded, False),
        (429, ProviderRateLimited, True),
        (500, ProviderUnavailable, True),
        (529, ProviderUnavailable, True),
        (400, ProviderBadResponse, False),
    ]
    for status_code, expected, retryable in cases:
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                status_code, json={"error": {"message": "synthetic failure"}}
            )
        )
        provider = fast_provider(AnthropicProvider(api_key(), max_retries=0))
        with pytest.raises(expected) as excinfo:
            await provider.generate(make_request())
        await provider.aclose()
        assert excinfo.value.retryable is retryable
        respx.reset()


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_retries_429_then_succeeds_with_backoff(monkeypatch) -> None:
    route = respx.post(ANTHROPIC_URL).mock(
        side_effect=[
            httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}}
            ),
            httpx.Response(200, json=anthropic_body()),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.providers.anthropic.asyncio.sleep", fake_sleep)
    provider = fast_provider(AnthropicProvider(api_key(), max_retries=3))
    response = await provider.generate(make_request())
    await provider.aclose()
    assert response.text == "pong"
    assert route.call_count == 2
    assert len(sleeps) == 1  # one backoff between the two attempts


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_backoff_is_jittered_and_capped(monkeypatch) -> None:
    respx.post(ANTHROPIC_URL).mock(
        side_effect=lambda request: httpx.Response(503, json={"error": {"message": "down"}})
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.providers.anthropic.asyncio.sleep", fake_sleep)
    provider = AnthropicProvider(api_key(), max_retries=3, backoff_base_s=0.5, backoff_cap_s=2.0)
    provider._rng.seed(1)
    with pytest.raises(ProviderUnavailable):
        await provider.generate(make_request())
    await provider.aclose()
    assert len(sleeps) == 3
    assert all(0.0 <= delay <= 2.0 for delay in sleeps)
    assert any(0.0 < delay <= 1.0 for delay in sleeps)  # jitter, not a fixed ladder
    assert max(sleeps) <= 2.0  # cap honoured


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_timeout_maps_to_provider_timeout() -> None:
    respx.post(ANTHROPIC_URL).mock(side_effect=httpx.TimeoutException("boom"))
    provider = fast_provider(AnthropicProvider(api_key(), max_retries=0))
    with pytest.raises(ProviderTimeout):
        await provider.generate(make_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_malformed_body_maps_to_bad_response() -> None:
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, text="not json at all"))
    provider = fast_provider(AnthropicProvider(api_key(), max_retries=0))
    with pytest.raises(ProviderBadResponse):
        await provider.generate(make_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_count_tokens_is_best_effort() -> None:
    respx.post("https://api.anthropic.com/v1/messages/count_tokens").mock(
        return_value=httpx.Response(200, json={"input_tokens": 42})
    )
    provider = AnthropicProvider(api_key())
    assert await provider.count_tokens(make_request()) == 42
    respx.post("https://api.anthropic.com/v1/messages/count_tokens").mock(
        return_value=httpx.Response(500, json={})
    )
    assert await provider.count_tokens(make_request()) is None
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_error_messages_are_redacted() -> None:
    key = api_key("Redact9this8key76")
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": f"bad key {key}"}})
    )
    provider = AnthropicProvider(key, max_retries=0)
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider.generate(make_request())
    await provider.aclose()
    assert key not in excinfo.value.message
    assert "[REDACTED]" in excinfo.value.message


# --------------------------------------------------------------------------- openai-compatible
def test_openai_compatible_satisfies_the_protocol() -> None:
    from app.providers.errors import ProviderUnsupported

    provider = OpenAICompatibleProvider(api_key(), base_url="https://x.example/v1")
    assert isinstance(provider, LLMProvider)
    assert OpenAICompatibleProvider.name == "openai_compatible"
    with pytest.raises(ProviderUnsupported):
        OpenAICompatibleProvider(api_key(), base_url="not-a-url")


@respx.mock
@pytest.mark.asyncio
async def test_openai_compatible_uses_base_url_and_auth_header() -> None:
    or_key = "sk-or-v1-sample-12345678"
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )
    )
    provider = OpenAICompatibleProvider(or_key, base_url="https://openrouter.example/api/v1")
    response = await provider.generate(make_request())
    await provider.aclose()
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {or_key}"
    body = json.loads(request.content)
    assert body["messages"][0] == {"role": "system", "content": "Stable system prefix."}
    assert response.text == "pong"


@respx.mock
@pytest.mark.asyncio
async def test_openai_compatible_usage_dedupes_cached_tokens() -> None:
    # VERIFY pin: prompt_tokens includes cached_tokens; TokenUsage must not.
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
        )
    )
    provider = OpenAICompatibleProvider(api_key(), base_url="https://openrouter.example/api/v1")
    response = await provider.generate(make_request())
    await provider.aclose()
    assert response.usage.in_ == 60
    assert response.usage.cached == 40
    assert response.usage.out == 10
    assert response.usage.total == 110


@respx.mock
@pytest.mark.asyncio
async def test_openai_compatible_response_format_fallback() -> None:
    """A service that rejects response_format still gets a schema-shaped call."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(
                400, json={"error": {"message": "response_format is not supported"}}
            )
        assert "JSON Schema" in body["messages"][0]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}
        )

    respx.post(OPENAI_URL).mock(side_effect=handler)
    provider = OpenAICompatibleProvider(api_key(), base_url="https://openrouter.example/api/v1")
    response = await provider.generate(make_request(schema=True))
    await provider.aclose()
    assert response.text == '{"ok": true}'


@respx.mock
@pytest.mark.asyncio
async def test_openai_compatible_auth_error_is_not_retried() -> None:
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "nope"}})
    )
    provider = fast_provider(
        OpenAICompatibleProvider(api_key(), base_url="https://openrouter.example/api/v1")
    )
    with pytest.raises(ProviderAuthError):
        await provider.generate(make_request())
    await provider.aclose()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_openai_compatible_count_tokens_is_unknown() -> None:
    provider = OpenAICompatibleProvider(api_key(), base_url="https://openrouter.example/api/v1")
    assert await provider.count_tokens(make_request()) is None
    await provider.aclose()
