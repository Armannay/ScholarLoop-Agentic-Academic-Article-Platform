"""MockProvider: scripting, failure injection and deterministic token accounting.

This is the provider every other step's tests will use, so its contract is pinned
here: per-role call indices, ``chars / 4`` tokens, and the three injectable failures.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.enums import BindingRole
from app.contracts.llm import LLMRequest
from app.providers import mock as mock_module
from app.providers.base import LLMProvider, estimate_tokens, prompt_tokens
from app.providers.errors import (
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnsupported,
)
from app.providers.mock import (
    DEFAULT_MODEL,
    FAILURE_MODES,
    MALFORMED_JSON_TEXT,
    FailureMode,
    MockFailure,
    MockProvider,
    Script,
    reply_key,
)
from tests.factories import make_request, sample_ulid

pytestmark = pytest.mark.providers


# ------------------------------------------------------------------ interface
def test_mock_satisfies_the_provider_protocol() -> None:
    """The mock is a real implementation, not a test-only lookalike."""
    assert isinstance(MockProvider(), LLMProvider)
    assert MockProvider.name == "mock"


def test_generate_is_async() -> None:
    """Providers are async-only: every call is I/O."""
    assert inspect.iscoroutinefunction(MockProvider.generate)


def test_mock_has_no_network_dependency() -> None:
    """HARD RULE 5 made structural: the mock cannot reach the network even by accident."""
    tree = ast.parse(Path(mock_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "httpx",
        "requests",
        "aiohttp",
        "socket",
        "urllib",
        "urllib3",
        "openai",
        "anthropic",
    }
    assert not (imported & forbidden), f"MockProvider imports {sorted(imported & forbidden)}"


def test_mock_generate_takes_no_credential_argument() -> None:
    """The only way to pass a key to a provider is... there is none."""
    parameters = set(inspect.signature(MockProvider.generate).parameters)
    assert parameters == {"self", "request", "model", "role", "account_id"}


# ------------------------------------------------------------------ scripting
async def test_scripted_replies_are_keyed_by_role_and_call_index() -> None:
    """(role, index) is the key - the same role calling twice gets different replies."""
    script = (
        Script()
        .reply(BindingRole.WRITER, 0, "first draft")
        .reply(BindingRole.WRITER, 1, "second draft")
        .reply(BindingRole.REVIEWER, 0, "review notes")
    )
    provider = MockProvider(script)

    first = await provider.generate(make_request(), role="writer")
    second = await provider.generate(make_request(), role="writer")
    review = await provider.generate(make_request(), role="reviewer")

    assert (first.text, second.text, review.text) == ("first draft", "second draft", "review notes")
    assert [call.role for call in provider.calls] == ["writer", "writer", "reviewer"]
    assert [call.index for call in provider.calls] == [0, 1, 0]
    assert provider.calls[0].served_scripted and provider.calls[-1].served_scripted


async def test_unscripted_calls_return_an_obvious_stub() -> None:
    """A forgotten script must not fail the test mysteriously."""
    provider = MockProvider()
    response = await provider.generate(make_request(), role="architect")
    assert "unscripted" in response.text
    assert provider.calls[0].served_scripted is False


async def test_json_replies_are_parseable_and_carry_the_schema_flag() -> None:
    """Structured calls answer with JSON, so callers can validate as usual."""
    provider = MockProvider(
        Script().json_reply(BindingRole.PLANNER, 0, {"version": 1, "tasks": []})
    )
    response = await provider.generate(make_request(schema=True), role="planner")
    assert json.loads(response.text) == {"version": 1, "tasks": []}

    unscripted = await MockProvider().generate(make_request(schema=True), role="planner")
    assert json.loads(unscripted.text)["schema_requested"] is True


async def test_script_can_be_extended_after_construction() -> None:
    """The common test pattern: build, then add the reply the assertion needs."""
    provider = MockProvider()
    provider.script_reply(BindingRole.VALIDATOR, 0, "Q=81")
    response = await provider.generate(make_request(), role="validator")
    assert response.text == "Q=81"


async def test_provider_without_schema_support_reports_unsupported() -> None:
    """A vendor that cannot do structured output must say so, not silently ignore it."""
    provider = MockProvider(supports_json_schema=False)
    with pytest.raises(ProviderUnsupported):
        await provider.generate(make_request(schema=True), role="writer")


# ------------------------------------------------------------ failure injection
async def test_timeout_injection() -> None:
    """timeout is retryable and distinguishable from every other failure."""
    provider = MockProvider(Script().fail(BindingRole.RESEARCHER, 0, FailureMode.TIMEOUT))
    with pytest.raises(ProviderTimeout) as excinfo:
        await provider.generate(make_request(), role="researcher")
    assert excinfo.value.retryable is True
    assert excinfo.value.provider == "mock"


async def test_rate_limit_injection_carries_retry_after() -> None:
    """429 handling needs the retry hint the orchestrator will honour."""
    provider = MockProvider(
        Script().fail(BindingRole.WRITER, 0, FailureMode.RATE_LIMIT, retry_after_s=2.5)
    )
    with pytest.raises(ProviderRateLimited) as excinfo:
        await provider.generate(make_request(), role="writer")
    assert excinfo.value.retry_after_s == 2.5
    assert excinfo.value.http_status == 429


async def test_malformed_json_injection_returns_text_the_caller_must_reject() -> None:
    """A model that ignores the schema is a *caller* problem: the mock raises for it."""
    provider = MockProvider(Script().fail(BindingRole.PLANNER, 0, FailureMode.MALFORMED_JSON))
    with pytest.raises(ProviderBadResponse) as excinfo:
        await provider.generate(make_request(schema=True), role="planner")
    assert MALFORMED_JSON_TEXT not in str(excinfo.value)  # the body is not echoed blindly
    assert excinfo.value.retryable is False


async def test_failed_attempts_are_recorded_too() -> None:
    """The call log describes the run, failures included: error_streak counts attempts."""
    provider = MockProvider(Script().fail(BindingRole.WRITER, 0, FailureMode.TIMEOUT))
    with pytest.raises(ProviderTimeout):
        await provider.generate(make_request(), role="writer")
    assert provider.call_count == 1
    assert [call.failed_with for call in provider.failed_calls] == [FailureMode.TIMEOUT]


async def test_intermittent_failure_then_success() -> None:
    """``times=1`` models 'the retry succeeds' without making the test flaky."""
    provider = MockProvider(
        Script()
        .fail(BindingRole.WRITER, 0, FailureMode.RATE_LIMIT, times=1)
        .reply(BindingRole.WRITER, 1, "draft after retry")
    )
    with pytest.raises(ProviderRateLimited):
        await provider.generate(make_request(), role="writer")
    second = await provider.generate(make_request(), role="writer")
    assert second.text == "draft after retry"
    assert provider.call_count == 2


async def test_failure_until_times_is_exhausted() -> None:
    """A persistent failure keeps failing, which is what STOP_ERRORS exists for."""
    provider = MockProvider(Script().fail(None, 0, FailureMode.TIMEOUT, times=2))
    for _ in range(2):
        with pytest.raises(ProviderTimeout):
            await provider.generate(make_request(), role="writer")
    recovered = await provider.generate(make_request(), role="writer")
    assert "unscripted" in recovered.text


async def test_fail_next_targets_the_next_call_of_that_role_only() -> None:
    """Failure injection is scoped per role, so one role's failure cannot leak into another."""
    provider = MockProvider()
    provider.fail_next(FailureMode.TIMEOUT, role=BindingRole.WRITER)

    other_role = await provider.generate(make_request(), role="reviewer")
    assert "unscripted" in other_role.text

    with pytest.raises(ProviderTimeout):
        await provider.generate(make_request(), role="writer")

    after = await provider.generate(make_request(), role="writer")
    assert "unscripted" in after.text


async def test_fail_next_without_a_role_fails_the_next_call_of_any_role() -> None:
    """A global injection is the way to test a provider-wide outage."""
    provider = MockProvider()
    provider.fail_next(FailureMode.RATE_LIMIT)
    with pytest.raises(ProviderRateLimited):
        await provider.generate(make_request(), role="researcher")


def test_failure_mode_validation() -> None:
    """A typo in a test's failure mode fails at construction, not at call time."""
    assert FAILURE_MODES == ("timeout", "rate_limit", "malformed_json")
    with pytest.raises(ValidationError, match="mode must be one of"):
        MockFailure(call_index=0, mode="explode")


# ------------------------------------------------------------------ token maths
async def test_token_accounting_is_deterministic_chars_over_four() -> None:
    """The documented approximation, applied identically to prompt and completion."""
    provider = MockProvider(Script().reply(BindingRole.WRITER, 0, "x" * 40))
    response = await provider.generate(make_request(text="y" * 20), role="writer")

    assert response.usage.out == estimate_tokens("x" * 40) == 10
    assert response.usage.total == response.usage.in_ + response.usage.cached + response.usage.out
    assert provider.total_tokens().total == response.usage.total


async def test_cached_prompt_tokens_are_reported_separately() -> None:
    """A cache:true system block is billed as cached, never double-counted."""
    provider = MockProvider()
    request = LLMRequest(
        system_blocks=[
            {"text": "stable" * 20, "cache": True},
            {"text": "volatile" * 10, "cache": False},
        ],
        messages=[{"role": "user", "text": "go"}],
        max_output_tokens=64,
    )
    response = await provider.generate(request, role="writer")

    assert response.usage.cached == estimate_tokens("stable" * 20)
    assert response.usage.in_ == prompt_tokens(request) - response.usage.cached
    assert response.usage.cache_hit_ratio > 0.5


async def test_count_tokens_is_exact_for_the_shared_convention() -> None:
    """The mock can always answer, so the governor never has to guess."""
    provider = MockProvider()
    request = make_request()
    assert await provider.count_tokens(request) == prompt_tokens(request)


async def test_latency_and_model_are_deterministic() -> None:
    """Assertions on latency must not depend on wall-clock time."""
    provider = MockProvider(latency_ms=11)
    first = await provider.generate(make_request(), role="writer", model="tier-mid")
    second = await provider.generate(make_request(), role="writer")
    assert (first.latency_ms, second.latency_ms) == (11, 12)
    assert first.model == "tier-mid"
    assert second.model == DEFAULT_MODEL  # provider default applies when none is given


async def test_reset_clears_calls_but_keeps_the_script() -> None:
    """Reuse one provider across scenarios without losing its replies."""
    provider = MockProvider(Script().reply(BindingRole.WRITER, 0, "draft"))
    await provider.generate(make_request(), role="writer")
    provider.reset()
    assert provider.call_count == 0
    again = await provider.generate(make_request(), role="writer")
    assert again.text == "draft"


async def test_aclose_is_idempotent() -> None:
    """Callers can close unconditionally."""
    provider = MockProvider()
    await provider.aclose()
    await provider.aclose()


async def test_parallel_calls_keep_per_role_indices() -> None:
    """max_parallel > 1 must not scramble the script: indices are per role."""
    provider = MockProvider(
        Script()
        .reply(BindingRole.WRITER, 0, "w0")
        .reply(BindingRole.WRITER, 1, "w1")
        .reply(BindingRole.RESEARCHER, 0, "r0")
        .reply(BindingRole.RESEARCHER, 1, "r1")
    )
    results = await asyncio.gather(
        provider.generate(make_request(), role="writer"),
        provider.generate(make_request(), role="researcher"),
        provider.generate(make_request(), role="writer"),
        provider.generate(make_request(), role="researcher"),
    )
    assert sorted(result.text for result in results) == ["r0", "r1", "w0", "w1"]
    assert [call.index for call in provider.calls_for("writer")] == [0, 1]


async def test_account_id_is_accepted_and_ignored() -> None:
    """The mock never reads a credential: billing identity is the caller's business."""
    provider = MockProvider()
    response = await provider.generate(make_request(), role="writer", account_id=sample_ulid(7))
    assert response.text


def test_reply_key_normalises_roles() -> None:
    """Enum and string roles address the same script slot."""
    assert reply_key(BindingRole.WRITER, 2) == reply_key("writer", 2) == ("writer", 2)


def test_provider_errors_are_scharloop_errors() -> None:
    """Every provider failure is catchable as one family, and carries a code."""
    assert issubclass(ProviderTimeout, ProviderError)
    assert ProviderTimeout("x", provider="mock").to_dict()["code"] == "provider_timeout"
