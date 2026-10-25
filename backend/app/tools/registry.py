"""Construct the Step 6 tool set through the existing scoped ToolRegistry."""

from __future__ import annotations

from pathlib import Path

import httpx

from app.core.policies import load_yaml_mapping
from app.tools.base import Tool, ToolRegistry
from app.tools.citation_check import CitationCheckTool
from app.tools.doi_verify import DoiVerifyTool
from app.tools.sanitize import SanitizeTool
from app.tools.scholar_search import ScholarSearchTool
from app.tools.text_checks import TextChecksTool

__all__ = ["build_tool_registry"]


def build_tool_registry(
    *,
    policy_path: Path | None = None,
    search_cache_path: Path | None = None,
    http_client: httpx.AsyncClient | None = None,
    max_result_chars: int = 8_000,
) -> ToolRegistry:
    """Load root grants and register all deterministic research/validation tools.

    The policy is deny-by-default and is passed to the repository's existing
    ``ToolRegistry.from_mapping`` API; this factory does not implement a registry.
    """
    default_policy = Path(__file__).resolve().parents[3] / "config" / "tool_policy.yaml"
    policy = load_yaml_mapping(policy_path or default_policy)
    tools: list[Tool] = [
        ScholarSearchTool(cache_path=search_cache_path, client=http_client),
        DoiVerifyTool(client=http_client),
        CitationCheckTool(),
        TextChecksTool(),
        SanitizeTool(),
    ]
    return ToolRegistry.from_mapping(
        tools,
        policy,
        max_result_chars=max_result_chars,
    )
