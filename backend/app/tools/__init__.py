"""Scoped, role-authorized tool definitions and execution boundary."""

from __future__ import annotations

from app.tools.base import (
    DEFAULT_ALLOWED_TOOLS_BY_ROLE,
    ScopedToolAccess,
    Tool,
    ToolDefinitionError,
    ToolDisabledError,
    ToolNotPermittedError,
    ToolRegistry,
    ToolScope,
    ToolScopeError,
    UnknownToolError,
    UntrustedToolResult,
)

__all__ = [
    "DEFAULT_ALLOWED_TOOLS_BY_ROLE",
    "ScopedToolAccess",
    "Tool",
    "ToolDefinitionError",
    "ToolDisabledError",
    "ToolNotPermittedError",
    "ToolRegistry",
    "ToolScope",
    "ToolScopeError",
    "UnknownToolError",
    "UntrustedToolResult",
]
