"""The root tool catalog and the one function that wires it for a run.

Registration is root-only and dependency-driven, and it fails closed:

* a tool is registered only when its dependencies exist (``lookup_source`` without a
  source store is simply absent, never a tool that returns empty data);
* the per-role allowlist comes from :data:`app.tools.base.DEFAULT_ALLOWED_TOOLS_BY_ROLE`
  and is applied by :class:`~app.tools.base.ToolRegistry`, so a role cannot gain access
  by asking the catalog for it;
* ``enabled_tools`` is a deployment/project switch that can only remove tools.

The catalog itself is metadata for operators and the UI: no secrets, no URLs, no
key-shaped values.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from app.contracts.enums import AgentRole
from app.tools.base import DEFAULT_ALLOWED_TOOLS_BY_ROLE, ToolRegistry
from app.tools.crossref import (
    CROSSREF_BASE_URL,
    CrossrefClient,
    CrossrefDoiTool,
    CrossrefSearchTool,
)
from app.tools.fetch import FetchUrlTool
from app.tools.http import (
    DEFAULT_TIMEOUT_S,
    HostResolver,
    HTTPClient,
    HttpxClient,
    SystemHostResolver,
)
from app.tools.library import LookupSourceTool, SourceLookup

__all__ = [
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ToolSpec",
    "build_tool_registry",
    "catalog",
    "roles_for_tool",
]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of one root tool, for docs and the API."""

    name: str
    summary: str
    token_cost_hint: int
    requires: tuple[str, ...]


TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="lookup_source",
        summary="Read a SourceCard already in the run's source store, by ULID or DOI.",
        token_cost_hint=250,
        requires=("source_store",),
    ),
    ToolSpec(
        name="crossref_doi",
        summary="Resolve one DOI to Crossref metadata; missing fields stay null and are listed.",
        token_cost_hint=400,
        requires=("http",),
    ),
    ToolSpec(
        name="search_sources",
        summary="Search Crossref bibliographic metadata for bounded candidate cards.",
        token_cost_hint=900,
        requires=("http",),
    ),
    ToolSpec(
        name="fetch_url",
        summary="Read a public web page as bounded text, refusing non-public addresses.",
        token_cost_hint=3_000,
        requires=("http", "dns"),
    ),
)

TOOL_NAMES: Final[frozenset[str]] = frozenset(spec.name for spec in TOOL_SPECS)


def roles_for_tool(name: str) -> tuple[AgentRole, ...]:
    """Roles allowed to call ``name``, alphabetically (the registry is authoritative)."""
    if name not in TOOL_NAMES:
        raise KeyError(f"unknown tool {name!r}")
    return tuple(
        sorted(
            (role for role in AgentRole if name in DEFAULT_ALLOWED_TOOLS_BY_ROLE[role]),
            key=lambda role: role.value,
        )
    )


def catalog() -> list[dict[str, Any]]:
    """JSON-friendly catalog rows; deterministic order, no runtime state."""
    return [
        {
            "name": spec.name,
            "summary": spec.summary,
            "roles": [role.value for role in roles_for_tool(spec.name)],
            "token_cost_hint": spec.token_cost_hint,
            "requires": list(spec.requires),
        }
        for spec in TOOL_SPECS
    ]


def build_tool_registry(
    *,
    http_client: HTTPClient | None = None,
    resolver: HostResolver | None = None,
    source_lookup: SourceLookup | None = None,
    crossref_base_url: str = CROSSREF_BASE_URL,
    crossref_mailto: str | None = None,
    enabled_tools: Collection[str] | None = None,
    max_result_bytes: int = 16_384,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ToolRegistry:
    """Build the root registry, registering only tools whose dependencies are present.

    Args:
        http_client: injected transport; defaults to :class:`HttpxClient` (no call is
            made until a tool runs, so constructing the catalog is safe offline).
        resolver: DNS resolver used by the egress guard; defaults to the system one.
        source_lookup: the run's source store; omit it and ``lookup_source`` is absent.
        crossref_base_url: override for tests/proxies; vendor detail stays in one place.
        crossref_mailto: optional polite-pool contact; pass it from configuration.
        enabled_tools: optional deployment allowlist; unknown names raise.
        max_result_bytes: registry-level cap applied after JSON serialisation/redaction.
        timeout_s: per-request timeout for both Crossref and page fetches.

    Raises:
        ValueError: ``enabled_tools`` names a tool that does not exist in the catalog.
    """
    known = set(TOOL_NAMES)
    selected = known if enabled_tools is None else set(enabled_tools)
    unknown = selected - known
    if unknown:
        raise ValueError(f"unknown tool(s): {', '.join(sorted(unknown))}")

    registry = ToolRegistry(max_result_bytes=max_result_bytes)
    client = http_client if http_client is not None else HttpxClient()

    if {"crossref_doi", "search_sources"} & selected:
        crossref = CrossrefClient(
            client,
            base_url=crossref_base_url,
            mailto=crossref_mailto,
            timeout_s=timeout_s,
        )
        if "crossref_doi" in selected:
            registry.register(CrossrefDoiTool(crossref))
        if "search_sources" in selected:
            registry.register(CrossrefSearchTool(crossref))

    if "fetch_url" in selected:
        registry.register(
            FetchUrlTool(
                client,
                resolver if resolver is not None else SystemHostResolver(),
                timeout_s=timeout_s,
            )
        )

    if "lookup_source" in selected and source_lookup is not None:
        registry.register(LookupSourceTool(source_lookup))

    return registry
