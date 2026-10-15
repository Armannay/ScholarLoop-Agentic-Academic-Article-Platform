"""Fixtures and sample payloads shared by the test-suite.

``sample_payloads()`` must cover every entry in the contract registry; a test asserts
that, so adding a contract without a sample fails loudly instead of quietly skipping
the round-trip check.

Everything here is synthetic: no real keys, no real sources, no network.
"""

from __future__ import annotations

from typing import Any, Final

from app.contracts.llm import LLMRequest
from app.contracts.quality import GATE_NAMES
from app.core.ids import new_ulid

__all__ = [
    "FIXED_TS_MS",
    "SAMPLE_BRIEF",
    "make_request",
    "sample_payloads",
    "sample_ulid",
    "sample_ulids",
]

FIXED_TS_MS: Final[int] = 1_700_000_000_000  # 2023-11-14T22:13:20Z


def sample_ulid(seed: int = 0) -> str:
    """Deterministic, valid ULID for fixtures."""
    return new_ulid(timestamp_ms=FIXED_TS_MS, entropy=bytes([seed % 256]) * 10)


def sample_ulids(count: int) -> list[str]:
    """``count`` distinct deterministic ULIDs."""
    return [sample_ulid(seed) for seed in range(count)]


SAMPLE_BRIEF: Final[dict[str, Any]] = {
    "topic": "Token-frugal orchestration for agentic academic writing",
    "article_type": "research",
    "audience": "graduate students and applied NLP practitioners",
    "language": "en",
    "target_words": 4000,
    "citation_style": "APA7",
    "must_include": ["a limitations section"],
    "must_avoid": ["claims about model capabilities without evidence"],
    "provided_data": ["Pilot run: 11 iterations, 612,400 tokens, Q 68 -> 81."],
}


def make_request(text: str = "Draft section introduction.", *, schema: bool = False) -> LLMRequest:
    """A minimal valid LLMRequest (optionally demanding JSON output)."""
    return LLMRequest(
        system_blocks=[{"text": "You are the writer agent.", "cache": True}],
        messages=[{"role": "user", "text": text}],
        max_output_tokens=512,
        json_schema={"type": "object"} if schema else None,
    )


def sample_payloads() -> dict[str, dict[str, Any]]:
    """One valid payload per contract: the input for round-trip and rejection tests."""
    ids = sample_ulids(12)
    task_id, other_task_id = ids[0], ids[1]
    source_id = ids[2]
    patch_id = ids[3]
    account_id = ids[4]
    second_account_id = ids[5]
    entry_id = ids[6]
    issue_id = "ISSUE-citations-001"

    patch_payload = {
        "section_id": "introduction",
        "base_version": 0,
        "patch_id": patch_id,
        "ops": [
            {"op": "replace_section", "text": "Agentic writing loops trade tokens for quality."},
            {
                "op": "insert_after",
                "anchor": "trade tokens for quality.",
                "text": " We measure it.",
            },
        ],
    }

    entry_payload = {
        "schema_version": 1,
        "id": entry_id,
        "iter": 3,
        "task_id": task_id,
        "role": "writer",
        "targets": ["introduction"],
        "summary": "Drafted the introduction from the architecture notes and the pilot run data.",
        "patch_ref": f"patch:{patch_id}",
        "tokens": {"in": 1200, "out": 340, "cached": 800},
        "issues_opened": [issue_id],
        "issues_closed": [],
        "status": "ok",
    }

    section_payload = {
        "schema_version": 1,
        "id": "introduction",
        "title": "Introduction",
        "order": 0,
        "version": 2,
        "hash": "sha256:" + "ab" * 32,
        "words": 412,
        "citations": [
            {"key": "(Nguyen, 2024)", "source_id": source_id},
            {"key": "(Petrova et al., 2023)", "source_id": None},
        ],
        "status": "draft",
        "open_issues": [issue_id],
    }

    source_payload = {
        "schema_version": 1,
        "id": source_id,
        "title": "Budgeted multi-agent writing with a deterministic controller",
        "authors": ["Nguyen, L.", "Alvarez, R."],
        "year": 2024,
        "venue": "Journal of Applied NLP Systems",
        "doi": "https://doi.org/10.1234/JANS.2024.00042",
        "url": "https://example.org/jans/2024/42",
        "snippet": (
            "The authors report that a bounded, reference-passing controller keeps prompt "
            "growth linear in the number of iterations while quality keeps improving."
        ),
        "verification": "verified",
    }

    report_payload = {
        "schema_version": 1,
        "iter": 3,
        "gates": dict.fromkeys(GATE_NAMES, True),
        "scores": {
            "contribution_and_gap": 72,
            "coherence": 68,
            "claims_supported": 61,
            "methods_clarity": 70,
            "results_match_claims": 66,
            "literature_coverage": 58,
            "language": 80,
            "consistency": 74,
            "limitations_and_ethics": 60,
        },
        "Q": 67,
        "verified_citation_ratio": 0.75,
        "issues": [
            {
                "id": issue_id,
                "section_id": "introduction",
                "severity": "high",
                "text": "(Petrova et al., 2023) has no matching reference entry.",
            }
        ],
    }

    return {
        "Brief": dict(SAMPLE_BRIEF),
        "Task": {
            "schema_version": 1,
            "id": task_id,
            "title": "Draft the introduction",
            "role": "writer",
            "section_ids": ["introduction"],
            "source_ids": [source_id],
            "instructions": "Open with the gap, then state the contribution.",
            "acceptance": ["No unsupported claims", "Ends with the contribution statement"],
            "token_budget": 6000,
            "depends_on": [other_task_id],
        },
        "Plan": {
            "schema_version": 1,
            "version": 1,
            "goal": "A verified 4,000-word research article at Q >= 70.",
            "tasks": [
                {
                    "schema_version": 1,
                    "id": other_task_id,
                    "title": "Collect source cards",
                    "role": "researcher",
                    "section_ids": ["introduction"],
                    "source_ids": [],
                    "instructions": "Find and card at least six primary sources with DOIs.",
                    "acceptance": ["Every card has a DOI or URL"],
                    "token_budget": 12000,
                    "depends_on": [],
                },
                {
                    "schema_version": 1,
                    "id": task_id,
                    "title": "Draft the introduction",
                    "role": "writer",
                    "section_ids": ["introduction"],
                    "source_ids": [source_id],
                    "instructions": "Use the cards from the researcher task.",
                    "acceptance": ["No unsupported claims"],
                    "token_budget": 6000,
                    "depends_on": [other_task_id],
                },
            ],
        },
        "SourceCard": dict(source_payload),
        "PatchOp": {
            "op": "replace_text",
            "old": "trade tokens for quality",
            "new": "trade budget for quality",
        },
        "Patch": patch_payload,
        "TokenUsage": {"in": 1200, "out": 340, "cached": 800},
        "ChangeEntry": entry_payload,
        "AgentResult": {
            "schema_version": 1,
            "entry": entry_payload,
            "patches": [patch_payload],
            "source_cards": [source_payload],
            "sections": [section_payload],
            "report": None,
        },
        "CitationRef": {"key": "(Nguyen, 2024)", "source_id": source_id},
        "SectionState": section_payload,
        "QualityIssue": {
            "id": issue_id,
            "section_id": "introduction",
            "severity": "med",
            "text": "The second paragraph repeats the abstract almost verbatim.",
        },
        "QualityReport": report_payload,
        "Verdict": {
            "schema_version": 1,
            "meets_minimum": False,
            "missing": ["A reference entry for (Petrova et al., 2023)."],
            "risks": ["Literature coverage is thin outside the pilot's own tooling."],
            "recommendation": "continue",
        },
        "LoopDecision": {
            "schema_version": 1,
            "iter": 3,
            "action": "CONTINUE",
            "reasons": ["q_below_minimum", "gates_failing"],
            "evidence": {"Q": 67, "q_min": 70, "failed_gates": ["citations_resolve_both_ways"]},
        },
        "MonthlyBudget": {"tokens": 2_000_000, "usd": 40.0},
        "Account": {
            "schema_version": 1,
            "id": account_id,
            "owner_id": "user-001",
            "provider": "anthropic",
            "label": "team-primary",
            "key_ciphertext": "v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "base_url": None,
            "status": "active",
            "monthly_budget": {"tokens": 2_000_000, "usd": None},
        },
        # AccountPublic is a redacted API view (a plain Contract, not a document):
        # no schema_version, because it is never stored.
        "AccountPublic": {
            "id": account_id,
            "owner_id": "user-001",
            "provider": "anthropic",
            "label": "team-primary",
            "status": "active",
            "base_url": None,
            "key_fingerprint": "A1B2C3D4E5F6",
            "monthly_budget": {"tokens": 2_000_000, "usd": None},
        },
        "AgentBinding": {
            "schema_version": 1,
            "role": "writer",
            "account_ids": [account_id, second_account_id],
            "strategy": "failover",
            "model": "claude-tier-mid",
            "max_input_tokens": 24_000,
            "max_output_tokens": 4_000,
            "enabled": True,
        },
        "SystemBlock": {"text": "You are the writer agent. Obey the rules file.", "cache": True},
        "LLMMessage": {"role": "user", "text": "Draft section introduction from these cards."},
        "LLMRequest": {
            "schema_version": 1,
            "system_blocks": [
                {"text": "You are the writer agent.", "cache": True},
                {"text": "Task: draft section introduction.", "cache": False},
            ],
            "messages": [{"role": "user", "text": "Card 1: Nguyen 2024."}],
            "max_output_tokens": 2_000,
            "json_schema": {"type": "object", "required": ["text"]},
        },
        "LLMResponse": {
            "schema_version": 1,
            "text": '{"text": "Introduction draft."}',
            "usage": {"in": 1200, "out": 340, "cached": 800},
            "model": "claude-tier-mid",
            "latency_ms": 812,
        },
    }
