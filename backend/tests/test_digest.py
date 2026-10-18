"""Tests for the bounded ``claude.md`` digest and :class:`~app.memory.digest.MemoryStore`.

Exercises ADR-0002 and HARD RULES 1, 3, 5:

* the last ``recent_window`` (3) ledger entries are rendered verbatim in ``claude.md``;
* compaction triggers when the digest reaches ``policies.compact_at_tokens`` (80% of
  ``digest_max_tokens``) or when ``force_compact=True``;
* the rendered digest never exceeds ``policies.digest_max_tokens`` even under heavy
  multi-iteration history;
* section states appear as metadata references only (never section prose);
* :class:`MemoryStore` coordinates ``AgentResult`` ingestion across ledger, sources,
  section states, quality reports and ``claude.md`` disk persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, BindingRole, SectionStatus, VerificationStatus
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.core.policies import Policies
from app.memory.digest import DEFAULT_DIGEST_FILENAME, DigestBuilder, DigestSnapshot, MemoryStore
from app.memory.errors import DigestBudgetExceeded
from app.memory.ledger import InMemoryLedger
from app.memory.sources import InMemorySourceStore
from app.providers.base import estimate_tokens
from app.providers.mock import MockProvider, Script
from tests.factories import sample_payloads, sample_ulid
from tests.test_ledger import make_entry
from tests.test_sources import make_card


def test_small_ledger_keeps_recent_entries_verbatim_without_compacting(
    policies: Policies,
) -> None:
    """When below compact_at_tokens and within recent_window, no compaction occurs."""
    ledger = InMemoryLedger(
        [
            make_entry(1, iteration=1, role=AgentRole.ARCHITECT, summary="Planned 8 sections."),
            make_entry(2, iteration=1, role=AgentRole.RESEARCHER, summary="Carded 6 DOI sources."),
            make_entry(3, iteration=2, role=AgentRole.WRITER, summary="Drafted introduction v1."),
        ]
    )
    builder = DigestBuilder(policies)
    snapshot = builder.build(ledger)

    assert snapshot.compacted is False
    assert snapshot.within_budget is True
    assert snapshot.tokens <= policies.digest_max_tokens
    assert snapshot.recent_entry_ids == [sample_ulid(1), sample_ulid(2), sample_ulid(3)]
    assert "Planned 8 sections." in snapshot.markdown
    assert "Carded 6 DOI sources." in snapshot.markdown
    assert "Drafted introduction v1." in snapshot.markdown


def test_compaction_triggers_at_80_percent_threshold_and_keeps_recent_window() -> None:
    """Exceeding compact_at_tokens (80% of digest_max_tokens) compacts older entries."""
    tight_policies = Policies(
        digest_max_tokens=300,
        recent_window=3,
        compact_trigger=0.8,
    )
    assert tight_policies.compact_at_tokens == 240

    # Create 10 entries with ~35-word summaries so uncompacted text exceeds 240 tokens
    entries = [
        make_entry(
            i,
            iteration=(i - 1) // 2 + 1,
            role=AgentRole.WRITER,
            summary=(
                f"Iteration step {i} refined the methodological narrative and linked "
                "verified empirical citations across the section structure cleanly."
            ),
            with_patch=True,
        )
        for i in range(1, 11)
    ]
    ledger = InMemoryLedger(entries)
    builder = DigestBuilder(tight_policies)
    snapshot = builder.build(ledger)

    assert snapshot.compacted is True
    assert snapshot.compacted_batch is not None
    assert snapshot.tokens <= tight_policies.digest_max_tokens
    assert estimate_tokens(snapshot.markdown) == snapshot.tokens
    assert snapshot.recent_entry_ids == [e.id for e in entries[-3:]]
    assert snapshot.compacted_entry_ids == [e.id for e in entries[:-3]]

    # The last 3 entries must appear verbatim in the rendered digest
    for recent_entry in entries[-3:]:
        assert recent_entry.id in snapshot.markdown
        assert recent_entry.summary in snapshot.markdown


def test_digest_never_exceeds_ceiling_under_heavy_load() -> None:
    """Even with 40 entries, 15 sources, sections and reports, digest <= digest_max_tokens."""
    policies = Policies(digest_max_tokens=500, recent_window=3, compact_trigger=0.8)
    payloads = sample_payloads()
    brief = Brief.model_validate(payloads["Brief"])
    plan = Plan.model_validate(payloads["Plan"])
    section = SectionState.model_validate(payloads["SectionState"])
    report = QualityReport.model_validate(payloads["QualityReport"])

    ledger = InMemoryLedger(
        [
            make_entry(
                i,
                iteration=(i - 1) // 4 + 1,
                summary=(
                    f"Completed task {i} touching introduction and methodology with "
                    "supporting evidence and structured patch operations."
                ),
                issues_opened=[f"ISSUE-test-{i:03d}"],
                with_patch=True,
            )
            for i in range(1, 41)
        ]
    )
    sources = InMemorySourceStore(
        [
            make_card(
                i,
                doi=f"10.1234/heavy.2025.{i:03d}",
                verification=(
                    VerificationStatus.VERIFIED if i % 2 == 0 else VerificationStatus.UNKNOWN
                ),
            )
            for i in range(1, 16)
        ]
    )

    builder = DigestBuilder(policies)
    snapshot = builder.build(
        ledger,
        brief=brief,
        plan=plan,
        sections=[section],
        sources=sources,
        reports=[report],
    )

    assert snapshot.within_budget
    assert snapshot.tokens <= policies.digest_max_tokens
    assert estimate_tokens(snapshot.markdown) <= policies.digest_max_tokens
    assert snapshot.compacted is True
    assert snapshot.latest_q == report.Q


async def test_memory_store_end_to_end_and_file_persistence(
    tmp_path: Path,
    policies: Policies,
) -> None:
    """MemoryStore ingests AgentResults, calls MockProvider compactor and writes claude.md."""
    payloads = sample_payloads()
    brief = Brief.model_validate(payloads["Brief"])
    plan = Plan.model_validate(payloads["Plan"])
    digest_file = tmp_path / DEFAULT_DIGEST_FILENAME

    memory = MemoryStore(policies, brief=brief, plan=plan, digest_path=digest_file)

    # 1. Researcher result adds a source card (unknown verification)
    card = make_card(1, doi="10.1234/jans.2025.001", verification=VerificationStatus.UNKNOWN)
    researcher_entry = make_entry(
        1,
        iteration=1,
        task_seed=1,
        role=AgentRole.RESEARCHER,
        targets=[card.id],
        summary="Collected primary empirical study on bounded digests.",
    )
    memory.record_agent_result(
        AgentResult(
            entry=researcher_entry,
            source_cards=[card],
        )
    )

    # 2. Writer result applies a patch reference and updates section state
    patch_id = sample_ulid(90)
    patch = Patch(
        section_id="introduction",
        base_version=0,
        patch_id=patch_id,
        ops=[PatchOp(op="replace_section", text="Empiricalprose" * 20)],
    )
    section_state = SectionState(
        id="introduction",
        title="Introduction",
        order=0,
        version=1,
        hash="sha256:" + "cd" * 32,
        words=180,
        status=SectionStatus.DRAFT,
    )
    writer_entry = make_entry(
        2,
        iteration=1,
        task_seed=0,
        role=AgentRole.WRITER,
        targets=["introduction"],
        summary="Drafted introduction section v1 citing card 1.",
    ).model_copy(update={"patch_ref": f"patch:{patch_id}"})
    memory.record_agent_result(
        AgentResult(
            entry=writer_entry,
            patches=[patch],
            sections=[section_state],
        )
    )

    # 3. Reviewer and Validator results across iterations 1..2
    reviewer_entry = make_entry(
        3,
        iteration=1,
        role=AgentRole.REVIEWER,
        targets=["introduction"],
        issues_opened=["ISSUE-cite-001"],
        summary="Reviewed introduction; flagged one unresolved citation marker.",
    )
    memory.record_agent_result(AgentResult(entry=reviewer_entry))

    verified_card = card.model_copy(update={"verification": VerificationStatus.VERIFIED})
    report = QualityReport.model_validate(payloads["QualityReport"])
    validator_entry = make_entry(
        4,
        iteration=2,
        role=AgentRole.VALIDATOR,
        targets=["introduction"],
        issues_closed=["ISSUE-cite-001"],
        summary="Verified DOI card and scored iteration 2 at Q=67.",
    )
    memory.record_agent_result(
        AgentResult(
            entry=validator_entry,
            source_cards=[verified_card],
            report=report,
        )
    )

    script = Script().json_reply(
        BindingRole.COMPACTOR,
        0,
        {"summary": "Researcher collected primary DOI card in iteration 1."},
    )
    provider = MockProvider(script)

    snapshot = await memory.abuild_digest(provider=provider, force_compact=True)
    assert snapshot.compacted is True
    assert snapshot.compacted_batch is not None
    assert snapshot.compacted_batch.used_llm is True
    assert snapshot.within_budget
    assert snapshot.latest_q == 67

    # HARD RULE 3: section body text from the patch must never appear in claude.md
    assert "Empiricalprose" not in snapshot.markdown
    assert "introduction | v1 | 180w | draft" in snapshot.markdown
    assert memory.sources.require(card.id).verification is VerificationStatus.VERIFIED

    assert digest_file.exists()
    assert digest_file.read_text(encoding="utf-8") == snapshot.markdown

    # Synchronous rebuild reuses prior_compacted and writes to directory target too
    sync_snapshot = memory.build_digest()
    assert sync_snapshot.within_budget
    assert 0.0 < sync_snapshot.utilization <= 1.0
    assert memory.last_snapshot == sync_snapshot

    written_in_dir = memory.builder.write(tmp_path, sync_snapshot)
    assert written_in_dir == tmp_path / DEFAULT_DIGEST_FILENAME


def test_write_rejects_oversized_snapshot(tmp_path: Path, policies: Policies) -> None:
    """DigestBuilder.write refuses to persist a snapshot that exceeds its max_tokens."""
    builder = DigestBuilder(policies)
    oversized = DigestSnapshot(
        markdown="x" * 10_000,
        tokens=2_500,
        max_tokens=2_000,
        compact_at_tokens=1_600,
    )
    with pytest.raises(DigestBudgetExceeded):
        builder.write(tmp_path / "claude.md", oversized)
