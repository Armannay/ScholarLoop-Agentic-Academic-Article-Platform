"""Tool boundary and deterministic research/validation tool implementations.

Concrete tools are exposed through the repository's scoped ``ToolRegistry``; the
registry remains the sole authority for schemas, permissions and invocation.
"""

from __future__ import annotations

from app.tools.base import (
    SUPPORTED_SCHEMA_KEYWORDS,
    ProjectToolScope,
    Tool,
    ToolArgumentError,
    ToolContext,
    ToolDisabled,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolRegistrationError,
    ToolRegistry,
    ToolResult,
    ToolUnauthorized,
    validate_json_arguments,
    validate_tool_schema,
)
from app.tools.citation_check import CitationCheckTool, verified_citation_ratio
from app.tools.doi_verify import DoiVerifyTool
from app.tools.registry import build_tool_registry
from app.tools.sanitize import SanitizedText, SanitizeTool, sanitize_untrusted_text
from app.tools.scholar_search import ScholarSearchTool, SQLiteQueryCache
from app.tools.text_checks import TextChecksTool

__all__ = [
    "SUPPORTED_SCHEMA_KEYWORDS",
    "CitationCheckTool",
    "DoiVerifyTool",
    "ProjectToolScope",
    "SQLiteQueryCache",
    "SanitizeTool",
    "SanitizedText",
    "ScholarSearchTool",
    "TextChecksTool",
    "Tool",
    "ToolArgumentError",
    "ToolContext",
    "ToolDisabled",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFound",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolResult",
    "ToolUnauthorized",
    "build_tool_registry",
    "sanitize_untrusted_text",
    "validate_json_arguments",
    "validate_tool_schema",
    "verified_citation_ratio",
]
