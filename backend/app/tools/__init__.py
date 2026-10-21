"""Tool boundary: the ``Tool`` protocol, its error family and the scoped registry.

Concrete tools belong to a later step. What ships here is the part every tool must
share: a JSON-schema'd ``run``, deny-by-default role and project scopes, argument
validation, untrusted result wrapping and a result-size cap.
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

__all__ = [
    "SUPPORTED_SCHEMA_KEYWORDS",
    "ProjectToolScope",
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
    "validate_json_arguments",
    "validate_tool_schema",
]
