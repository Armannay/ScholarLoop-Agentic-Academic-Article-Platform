"""Fail-closed bounds for large citation inputs."""

from __future__ import annotations

import pytest

from app.contracts.enums import AgentRole
from app.tools.base import ToolContext
from app.tools.citation_check import CitationCheckTool

pytestmark = pytest.mark.tools


async def test_too_many_expanded_citations_are_rejected() -> None:
    result = await CitationCheckTool().run(
        {
            "iter": 0,
            "citation_style": "IEEE",
            "sections": [{"id": "results", "text": "[1-50] " * 201}],
            "references": [],
            "source_cards": [],
        },
        context=ToolContext(role=AgentRole.VALIDATOR),
    )
    assert result.ok is False
    assert "too many" in result.content
