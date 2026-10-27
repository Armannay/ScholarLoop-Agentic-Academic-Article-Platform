"""Tests for history compaction (:mod:`app.memory.compactor`).

Verifies ADR-0002, HARD RULE 3 and HARD RULE 5:

* deterministic compaction preserves every entry ULID, task ULID, role, status count,
  target, patch pointer and issue transition;
* LLM-assisted compaction calls :class:`~app.providers.mock.MockProvider` under
  ``BindingRole.COMPACTOR`` with a strict JSON schema and cacheable system block;
* injected provider failures (``timeout``, ``rate_limit``, ``malformed_json``) fall
  back cleanly to deterministic compaction when ``fallback_on_error=True`` and raise
  :class:`~app.memory.errors.CompactionError` when ``fallback_on_error=False``;
* rendered compacted blocks always respect their ``max_tokens`` ceiling.
"""

from __future__ import annotations

import pytest

from app.contracts.enums import ORCHESTRATOR_ROLE, AgentRole, BindingRole, ChangeStatus
from app.memory.compactor import (
    COMPACTOR_JSON_SCHEMA,
    DigestCompactor,
    deterministic_compact,
)
from app.memory.errors import CompactionError
from app.providers.base import estimate_tokens
from app.providers.mock import FailureMode, MockProvider, Script
from tests.test_ledger import make_entry


def test_deterministic_compact_preserves_ids_statuses_and_tokens() -> None:
    """Every entry_id, task_id, status and token count survives compaction."""
    entries = [
        make_entry(
            1,
            iteration=1,
            task_seed=10,
            role=AgentRole.ARCHITECT,
            targets=["introduction", "methodology"],
            tokens_in=300,
            tokens_out=100,
            tokens_cached=100,
        ),
        make_entry(
            2,
            iteration=1,
            task_seed=11,
            role=AgentRole.RESEARCHER,
            targets=["introduction"],
            issues_opened=["ISSUE-src-001"],
            tokens_in=500,
            tokens_out=150,
            tokens_cached=200,
        ),
        make_entry(
            3,
            iteration=2,
            task_seed=12,
            role=AgentRole.WRITER,
            targets=["introduction"],
            with_patch=True,
            issues_closed=["ISSUE-src-001"],
            status=ChangeStatus.OK,
            tokens_in=600,
            tokens_out=250,
            tokens_cached=300,
        ),
    ]

    batch = deterministic_compact(entries)
    assert batch.iter_start == 1
    assert batch.iter_end == 2
    assert batch.entry_count == 3
    assert batch.entry_ids == [e.id for e in entries]
    assert batch.task_ids == [e.task_id for e in entries]
    assert batch.roles == [AgentRole.ARCHITECT, AgentRole.RESEARCHER, AgentRole.WRITER]
    assert batch.statuses == {"ok": 3}
    assert batch.targets == ["introduction", "methodology"]
    assert len(batch.patch_refs) == 1
    assert batch.issues_opened == ["ISSUE-src-001"]
    assert batch.issues_closed == ["ISSUE-src-001"]
    assert batch.tokens.total == 2_500
    assert batch.used_llm is False
    assert batch.compactor_usage is None

    rendered = batch.render()
    for entry in entries:
        assert entry.id in rendered


def test_deterministic_compact_keeps_orchestrator_entries_addressable() -> None:
    """Rollback/emergency entries compact like agent entries: ids, roles, no bodies."""
    entries = [
        make_entry(1, iteration=1, task_seed=10, role=AgentRole.WRITER),
        make_entry(
            2,
            iteration=2,
            task_seed=11,
            role=ORCHESTRATOR_ROLE,
            summary="Rolled back to the best snapshot after erosion.",
        ),
    ]

    batch = deterministic_compact(entries)
    assert batch.roles == [AgentRole.WRITER, ORCHESTRATOR_ROLE]
    assert batch.entry_ids == [e.id for e in entries]
    rendered = batch.render()
    assert ORCHESTRATOR_ROLE in rendered
    assert entries[1].id in rendered


def test_deterministic_compact_rejects_empty_entries() -> None:
    """Compacting zero entries is a caller bug and raises CompactionError."""
    with pytest.raises(CompactionError, match="empty"):
        deterministic_compact([])


async def test_compactor_uses_mock_provider_with_compactor_role() -> None:
    """When given a MockProvider, DigestCompactor calls role='compactor' with JSON schema."""
    entries = [
        make_entry(1, iteration=1, role=AgentRole.ARCHITECT),
        make_entry(2, iteration=1, role=AgentRole.RESEARCHER),
    ]
    script = Script().json_reply(
        BindingRole.COMPACTOR,
        0,
        {
            "summary": (
                "Iteration 1 established the section structure and collected verified "
                "source cards for the introduction."
            )
        },
    )
    provider = MockProvider(script)
    compactor = DigestCompactor()

    batch = await compactor.compact(entries, provider=provider, model="mock-cheap-1")
    assert batch.used_llm is True
    assert "Iteration 1 established the section structure" in batch.summary
    assert batch.entry_ids == [entries[0].id, entries[1].id]
    assert batch.compactor_usage is not None
    assert batch.compactor_usage.total > 0

    calls = provider.calls_for(BindingRole.COMPACTOR)
    assert len(calls) == 1
    assert calls[0].model == "mock-cheap-1"
    assert calls[0].served_scripted is True

    req = compactor.build_request(entries)
    assert req.json_schema == COMPACTOR_JSON_SCHEMA
    assert req.system_blocks[0].cache is True


@pytest.mark.parametrize(
    "mode",
    [FailureMode.TIMEOUT, FailureMode.RATE_LIMIT, FailureMode.MALFORMED_JSON],
)
async def test_compactor_falls_back_on_provider_failures(mode: FailureMode) -> None:
    """Injected provider failures fall back to deterministic compaction by default."""
    entries = [make_entry(1, iteration=1), make_entry(2, iteration=2)]
    provider = MockProvider(Script().fail(BindingRole.COMPACTOR, 0, mode))

    compactor = DigestCompactor(fallback_on_error=True)
    batch = await compactor.compact(entries, provider=provider)
    assert batch.used_llm is False
    assert batch.entry_ids == [entries[0].id, entries[1].id]
    assert len(provider.failed_calls) == 1


@pytest.mark.parametrize(
    "mode",
    [FailureMode.TIMEOUT, FailureMode.RATE_LIMIT, FailureMode.MALFORMED_JSON],
)
async def test_compactor_raises_when_fallback_disabled(mode: FailureMode) -> None:
    """With fallback_on_error=False, provider failures surface as CompactionError."""
    entries = [make_entry(1, iteration=1)]
    provider = MockProvider(Script().fail(BindingRole.COMPACTOR, 0, mode))
    compactor = DigestCompactor(fallback_on_error=False)

    with pytest.raises(CompactionError):
        await compactor.compact(entries, provider=provider)


def test_compacted_batch_render_respects_tight_token_caps() -> None:
    """CompactedBatch.render(max_tokens=...) stays within the requested token cap."""
    entries = [
        make_entry(
            i,
            iteration=(i - 1) // 3 + 1,
            summary=" ".join(["word"] * 45),
            with_patch=True,
        )
        for i in range(1, 15)
    ]
    batch = deterministic_compact(entries)

    for cap in (300, 120, 60, 30):
        rendered = batch.render(max_tokens=cap)
        assert estimate_tokens(rendered) <= cap


async def test_compactor_prior_summary_and_schema_validation_guards() -> None:
    """DigestCompactor validates max_output_tokens, prior_summary and bad JSON shapes."""
    with pytest.raises(CompactionError, match="max_output_tokens"):
        DigestCompactor(max_output_tokens=16)

    compactor = DigestCompactor(fallback_on_error=False)
    with pytest.raises(CompactionError, match="zero entries"):
        compactor.build_request([])

    entries = [make_entry(1, iteration=1)]
    req = compactor.build_request(entries, prior_summary="Iteration 0 baseline.")
    assert "Iteration 0 baseline." in req.messages[0].text

    # Unscripted MockProvider returns stub JSON without 'summary', triggering CompactionError
    unscripted_provider = MockProvider()
    with pytest.raises(CompactionError, match="summary"):
        await compactor.compact(entries, provider=unscripted_provider)

    # Wrong JSON schema shape triggers CompactionError when fallback_on_error=False
    bad_schema_provider = MockProvider(
        Script().json_reply(BindingRole.COMPACTOR, 0, {"unexpected": "field"})
    )
    with pytest.raises(CompactionError, match="schema validation"):
        await compactor.compact(entries, provider=bad_schema_provider)
