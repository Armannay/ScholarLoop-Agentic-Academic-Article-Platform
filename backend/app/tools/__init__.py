"""Scoped, role-authorized tool definitions and execution boundary.

The runtime surface is :class:`ToolRegistry` / :class:`ScopedToolAccess`; concrete
root tools live in the sibling modules and are wired by :func:`build_tool_registry`.
"""

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
from app.tools.catalog import TOOL_NAMES, TOOL_SPECS, ToolSpec, build_tool_registry, catalog
from app.tools.crossref import CrossrefClient, CrossrefDoiTool, CrossrefSearchTool
from app.tools.errors import (
    HostResolutionError,
    HTTPFetchError,
    ToolArgumentError,
    ToolError,
    ToolURLBlocked,
)
from app.tools.fetch import FetchUrlTool, html_to_text
from app.tools.http import (
    HostResolver,
    HTTPClient,
    HTTPResponse,
    HttpxClient,
    SystemHostResolver,
    ensure_public_url,
)
from app.tools.library import LookupSourceTool, SourceLookup

__all__ = [
    "DEFAULT_ALLOWED_TOOLS_BY_ROLE",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "CrossrefClient",
    "CrossrefDoiTool",
    "CrossrefSearchTool",
    "FetchUrlTool",
    "HTTPClient",
    "HTTPFetchError",
    "HTTPResponse",
    "HostResolutionError",
    "HostResolver",
    "HttpxClient",
    "LookupSourceTool",
    "ScopedToolAccess",
    "SourceLookup",
    "SystemHostResolver",
    "Tool",
    "ToolArgumentError",
    "ToolDefinitionError",
    "ToolDisabledError",
    "ToolError",
    "ToolNotPermittedError",
    "ToolRegistry",
    "ToolScope",
    "ToolScopeError",
    "ToolSpec",
    "ToolURLBlocked",
    "UnknownToolError",
    "UntrustedToolResult",
    "build_tool_registry",
    "catalog",
    "ensure_public_url",
    "html_to_text",
]
