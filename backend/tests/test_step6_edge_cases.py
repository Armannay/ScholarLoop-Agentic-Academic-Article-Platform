"""Additional edge cases for deterministic citation and text checks."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.contracts.enums import AgentRole
from app.tools.base import ToolContext
from app.tools.citation_check import CitationCheckTool
from app.tools.text_checks import TextChecksTool
from tests.factories import sample_ulid

pytestmark = pytest.mark.tools
CONTEXT = ToolContext(role=AgentRole.VALIDATOR)


def args(
    text: str, references: Any, cards: list[dict[str, str]], style: str = "APA7"
) -> dict[str, Any]:
    return {
        "iter": 0,
        "citation_style": style,
        "sections": [{"id": "introduction", "text": text}],
        "references": references,
        "source_cards": cards,
    }


async def test_wrong_style_citations_are_still_checked_for_orphans() -> None:
    result = await CitationCheckTool().run(args("Unresolved claim [4].", [], []), context=CONTEXT)
    payload = json.loads(result.content)
    assert "4" in payload["orphan_citations"]
    assert payload["report"]["gates"]["citation_style_matches"] is False
    assert payload["report"]["gates"]["citations_resolve_both_ways"] is False


async def test_ambiguous_reference_ids_cannot_inflate_ratio() -> None:
    first, second = sample_ulid(31), sample_ulid(32)
    references = [
        {"key": "Smith, 2021", "text": "Smith, A. (2021). Paper.", "source_id": first},
        {"key": "Smith, 2021", "text": "Smith, B. (2021). Paper.", "source_id": second},
    ]
    cards = [{"id": first, "verification": "verified"}, {"id": second, "verification": "unknown"}]
    result = await CitationCheckTool().run(
        args("(Smith, 2021)", references, cards), context=CONTEXT
    )
    payload = json.loads(result.content)
    assert payload["verified_citation_ratio"] == 0.0
    assert payload["duplicate_references"]
    assert payload["report"]["gates"]["citations_resolve_both_ways"] is False


async def test_reference_with_conflicting_embedded_ids_is_unresolved() -> None:
    first, second = sample_ulid(33), sample_ulid(34)
    references = [
        {
            "key": "Smith, 2021",
            "text": f"Smith, A. (2021). Paper. [[CITE:{second}]]",
            "source_id": first,
        }
    ]
    result = await CitationCheckTool().run(
        args(f"[[CITE:{first}]]", references, [{"id": first, "verification": "verified"}]),
        context=CONTEXT,
    )
    payload = json.loads(result.content)
    assert any("ambiguous source ids" in item for item in payload["unresolved_ids"])
    assert payload["report"]["gates"]["citations_resolve_both_ways"] is False


async def test_standalone_lorem_is_a_placeholder_and_empty_heading_is_missing() -> None:
    payload = {
        "iter": 0,
        "article_type": "research",
        "target_words": 300,
        "sections": [
            {"id": "introduction", "text": "## Introduction\n"},
            {"id": "results", "text": "lorem"},
        ],
    }
    result = await TextChecksTool().run(payload, context=CONTEXT)
    metrics = json.loads(result.content)["metrics"]
    assert metrics["placeholder_count"] == 1
    assert "introduction" in metrics["missing_sections"]
    assert metrics["empty_heading_count"] == 1
