"""Step 6: concrete root tools, the per-role allowlist and the egress guard.

Every test injects a fake HTTP client and a fake DNS resolver: no live call is made
anywhere in this file (HARD RULE 5). Crossref responses are synthetic, shaped like the
recorded API payloads; a field the fixture omits must stay ``None`` in the tool output
(HARD RULE 1).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.contracts.enums import AgentRole
from app.contracts.source import SourceCard
from app.core.redaction import find_secrets
from app.core.settings import Settings
from app.tools.base import DEFAULT_ALLOWED_TOOLS_BY_ROLE, ToolDisabledError
from app.tools.catalog import (
    TOOL_NAMES,
    TOOL_SPECS,
    build_tool_registry,
    catalog,
    roles_for_tool,
)
from app.tools.crossref import CrossrefClient, CrossrefDoiTool, CrossrefSearchTool
from app.tools.errors import (
    HostResolutionError,
    HTTPFetchError,
    ToolArgumentError,
    ToolURLBlocked,
)
from app.tools.fetch import FetchUrlTool, html_to_text
from app.tools.http import HTTPResponse, HttpxClient, ensure_public_url
from app.tools.library import LookupSourceTool
from tests.factories import sample_payloads

PUBLIC_IP = "93.184.216.34"


class FakeHTTPClient:
    """Deterministic transport double; records what would have been requested."""

    def __init__(self, responder: Callable[[str], HTTPResponse]) -> None:
        self._responder = responder
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 20.0,
    ) -> HTTPResponse:
        self.calls.append((url, dict(params or {})))
        return self._responder(url)


class FakeResolver:
    """Map hostnames to addresses; the default is one public address."""

    def __init__(self, mapping: Mapping[str, tuple[str, ...]] | None = None) -> None:
        self.mapping = dict(mapping or {})

    async def resolve(self, host: str) -> tuple[str, ...]:
        return self.mapping.get(host, (PUBLIC_IP,))


class FakeSourceLookup:
    """Two-method store double, matching the ``SourceLookup`` Protocol."""

    def __init__(self, cards: list[SourceCard]) -> None:
        self.cards = {card.id: card for card in cards}
        self.doi_requests: list[str] = []

    def get(self, source_id: str) -> SourceCard | None:
        return self.cards.get(source_id)

    def find_by_doi(self, doi: str) -> SourceCard | None:
        self.doi_requests.append(doi)
        return next((card for card in self.cards.values() if card.doi == doi), None)


def http_response(
    body: object,
    *,
    status_code: int = 200,
    content_type: str = "application/json",
    url: str = "https://api.crossref.org/works/10.1234/abc",
    location: str | None = None,
) -> HTTPResponse:
    text = body if isinstance(body, str) else json.dumps(body)
    return HTTPResponse(
        status_code=status_code,
        text=text,
        content_type=content_type,
        final_url=url,
        location=location,
    )


def crossref_payload(**work_overrides: object) -> dict[str, Any]:
    work: dict[str, Any] = {
        "DOI": "10.1234/abc",
        "title": ["A bounded controller for agentic writing"],
        "author": [
            {"family": "Nguyen", "given": "Linh"},
            {"name": "ScholarLoop Consortium"},
        ],
        "issued": {"date-parts": [[2024, 5, 2]]},
        "container-title": ["Journal of Applied NLP Systems"],
        "publisher": "Example Press",
        "type": "journal-article",
        "URL": "https://doi.org/10.1234/abc",
    }
    work.update(work_overrides)
    return {"status": "ok", "message-type": "work", "message": work}


def sample_card(**overrides: object) -> SourceCard:
    payload = dict(sample_payloads()["SourceCard"])
    payload.update(overrides)
    return SourceCard.model_validate(payload)


# --------------------------------------------------------------------------- catalog


def test_catalog_allowlist_and_specs_agree() -> None:
    """One source of truth: the registry allowlist and the catalog describe the same set."""
    assert {row["name"] for row in catalog()} == set(TOOL_NAMES)
    assert len(TOOL_SPECS) == len(TOOL_NAMES)

    for role in AgentRole:
        allowed = DEFAULT_ALLOWED_TOOLS_BY_ROLE[role]
        assert allowed <= TOOL_NAMES
        for name in allowed:
            assert role in roles_for_tool(name)

    assert DEFAULT_ALLOWED_TOOLS_BY_ROLE[AgentRole.ARCHITECT] == frozenset()
    assert roles_for_tool("lookup_source") == (
        AgentRole.RESEARCHER,
        AgentRole.REVIEWER,
        AgentRole.VALIDATOR,
        AgentRole.WRITER,
    )
    with pytest.raises(KeyError, match="unknown tool"):
        roles_for_tool("rm_rf")

    for row in catalog():
        assert set(row) == {"name", "summary", "roles", "token_cost_hint", "requires"}
        assert row["roles"] == sorted(row["roles"])


def test_registry_registers_by_dependency_and_fails_closed() -> None:
    """A missing store omits its tool; a role still cannot see a tool it was not granted."""
    lookup = FakeSourceLookup([sample_card()])
    transport = FakeHTTPClient(lambda url: http_response({}))

    without_store = build_tool_registry(http_client=transport, resolver=FakeResolver())
    assert {schema["name"] for schema in without_store.schemas_for("researcher")} == {
        "crossref_doi",
        "fetch_url",
        "search_sources",
    }
    assert without_store.schemas_for("writer") == []
    assert without_store.schemas_for("architect") == []

    with_store = build_tool_registry(
        http_client=transport,
        resolver=FakeResolver(),
        source_lookup=lookup,
    )
    assert [schema["name"] for schema in with_store.schemas_for("writer")] == ["lookup_source"]
    assert with_store.schemas_for("architect") == []
    assert {schema["name"] for schema in with_store.schemas_for("validator")} == {
        "crossref_doi",
        "fetch_url",
        "lookup_source",
    }

    only_doi = build_tool_registry(
        http_client=transport,
        enabled_tools={"crossref_doi"},
    )
    assert [schema["name"] for schema in only_doi.schemas_for("researcher")] == ["crossref_doi"]
    with pytest.raises(ValueError, match="unknown tool"):
        build_tool_registry(http_client=transport, enabled_tools={"unknown_tool"})


def test_tool_specs_match_tool_instances() -> None:
    """The advertised cost hint is the one the registry reports, not a stale copy."""
    lookup = FakeSourceLookup([sample_card()])
    registry = build_tool_registry(
        http_client=FakeHTTPClient(lambda url: http_response({})),
        resolver=FakeResolver(),
        source_lookup=lookup,
    )
    hints = {
        schema["name"]: schema["token_cost_hint"]
        for role in AgentRole
        for schema in registry.schemas_for(role)
    }
    for spec in TOOL_SPECS:
        assert hints[spec.name] == spec.token_cost_hint


# --------------------------------------------------------------------------- crossref


async def test_crossref_doi_copies_metadata_and_normalises_the_doi() -> None:
    """Metadata is copied field by field; the DOI prefix is stripped before lookup."""
    transport = FakeHTTPClient(lambda url: http_response(crossref_payload()))
    client = CrossrefClient(transport, mailto="ops@example.org")
    tool = CrossrefDoiTool(client)

    result = await tool.run({"doi": "https://doi.org/10.1234/ABC"})

    assert transport.calls[0][0] == "https://api.crossref.org/works/10.1234/abc"
    assert transport.calls[0][1]["mailto"] == "ops@example.org"
    assert result["status"] == "ok"
    assert result["title"] == "A bounded controller for agentic writing"
    assert result["authors"] == ["Nguyen, Linh", "ScholarLoop Consortium"]
    assert result["year"] == 2024
    assert result["venue"] == "Journal of Applied NLP Systems"
    assert result["doi"] == "10.1234/abc"
    assert result["complete"] is True
    assert result["missing"] == []
    assert result["locatable"] is True


async def test_crossref_doi_leaves_unknown_fields_empty_instead_of_guessing() -> None:
    """An incomplete record is reported as incomplete; no year, venue or author is invented."""
    payload = crossref_payload()
    message = payload["message"]
    message.update(
        {
            "title": ["Only a title is deposited"],
            "author": [],
            "issued": {},
            "container-title": [],
            "publisher": None,
            "URL": None,
        }
    )
    tool = CrossrefDoiTool(CrossrefClient(FakeHTTPClient(lambda url: http_response(payload))))

    result = await tool.run({"doi": "10.1234/abc"})

    assert result["status"] == "ok"
    assert result["title"] == "Only a title is deposited"
    assert result["authors"] == []
    assert result["year"] is None
    assert result["venue"] is None
    assert result["complete"] is False
    assert result["missing"] == ["year", "venue"]


async def test_crossref_doi_reports_miss_malformed_and_transport_failures() -> None:
    """A bad DOI is a miss, a broken response is unavailable, a timeout is unavailable."""

    def responder(url: str) -> HTTPResponse:
        if url.endswith("/404"):
            return http_response({"status": "error"}, status_code=404)
        if url.endswith("/broken"):
            return http_response("not json at all")
        raise HTTPFetchError("timeout")

    tool = CrossrefDoiTool(CrossrefClient(FakeHTTPClient(responder)))

    assert (await tool.run({"doi": "10.1234/404"}))["status"] == "not_found"
    malformed = await tool.run({"doi": "10.1234/broken"})
    assert malformed == {"status": "unavailable", "reason": "malformed_response"}
    timed_out = await tool.run({"doi": "10.1234/slow"})
    assert timed_out == {"status": "unavailable", "reason": "timeout"}
    with pytest.raises(ToolArgumentError, match="look like"):
        await tool.run({"doi": "not-a-doi"})
    with pytest.raises(ToolArgumentError, match="unknown argument"):
        await tool.run({"doi": "10.1234/abc", "api_key": "placeholder-value"})


async def test_crossref_search_is_bounded_and_never_fabricates() -> None:
    """Search forwards a hard row limit and returns only fields the response carried."""
    items = [
        crossref_payload()["message"],
        {
            "DOI": "10.1234/partial",
            "title": ["Partial record"],
            "issued": {"date-parts": [[2021]]},
        },
    ]
    transport = FakeHTTPClient(
        lambda url: http_response({"status": "ok", "message": {"items": items}})
    )
    tool = CrossrefSearchTool(CrossrefClient(transport))

    result = await tool.run({"query": "deterministic orchestration", "limit": 3})

    params = transport.calls[0][1]
    assert params["rows"] == "3"
    assert "select" in params
    assert params["query.bibliographic"] == "deterministic orchestration"
    assert result["status"] == "ok"
    assert result["count"] == 2
    partial = result["results"][1]
    assert partial["venue"] is None
    assert partial["authors"] == []
    assert partial["missing"] == ["venue"]
    assert partial["locatable"] is True

    no_hits = CrossrefSearchTool(
        CrossrefClient(FakeHTTPClient(lambda url: http_response({"message": {"items": []}})))
    )
    empty = await no_hits.run({"query": "nothing here"})
    assert empty["status"] == "not_found"
    assert empty["results"] == []
    with pytest.raises(ToolArgumentError, match="between 1 and 10"):
        await no_hits.run({"query": "x y", "limit": 99})


# --------------------------------------------------------------------------- egress


async def test_ensure_public_url_normalises_and_refuses_non_public_targets() -> None:
    """The guard normalises harmless spelling differences and blocks everything local."""
    resolver = FakeResolver(
        {
            "example.com": (PUBLIC_IP,),
            "private.example": ("10.1.2.3",),
            "mixed.example": (PUBLIC_IP, "127.0.0.1"),
            "empty.example": (),
        }
    )

    assert (
        await ensure_public_url("HTTPS://Example.COM/Path?q=1#frag", resolver=resolver)
        == "https://example.com/Path?q=1"
    )
    assert await ensure_public_url("https://example.com", resolver=resolver) == (
        "https://example.com/"
    )
    assert (
        await ensure_public_url("http://[2606:4700:4700::1111]/", resolver=resolver)
        == "http://[2606:4700:4700::1111]/"
    )

    for url in (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
        "http://service.internal/",
    ):
        with pytest.raises(ToolURLBlocked):
            await ensure_public_url(url, resolver=resolver)

    with pytest.raises(ToolURLBlocked, match="not publicly routable"):
        await ensure_public_url("http://private.example/", resolver=resolver)
    with pytest.raises(ToolURLBlocked, match="not publicly routable"):
        await ensure_public_url("http://mixed.example/", resolver=resolver)
    with pytest.raises(HostResolutionError, match="no addresses"):
        await ensure_public_url("http://empty.example/", resolver=resolver)


async def test_fetch_url_blocks_private_redirect_hops_before_requesting_them() -> None:
    """A public page redirecting to cloud metadata is refused, not followed."""
    transport = FakeHTTPClient(
        lambda url: http_response(
            "",
            status_code=302,
            content_type="text/html",
            location="http://169.254.169.254/latest/meta-data/",
        )
    )
    tool = FetchUrlTool(transport, FakeResolver())

    result = await tool.run({"url": "https://example.com/start"})

    assert result["status"] == "blocked"
    assert len(transport.calls) == 1


async def test_fetch_url_follows_a_safe_redirect_and_returns_text() -> None:
    """Redirects are followed manually, and the final page is reduced to text."""

    def responder(url: str) -> HTTPResponse:
        if url == "https://example.com/start":
            return http_response(
                "",
                status_code=301,
                content_type="text/html",
                url=url,
                location="/final",
            )
        return http_response(
            "<html><body><h1>Evidence</h1><script>alert('x')</script>"
            "<p>Body text</p></body></html>",
            content_type="text/html",
            url=url,
        )

    transport = FakeHTTPClient(responder)
    tool = FetchUrlTool(transport, FakeResolver())

    result = await tool.run({"url": "https://example.com/start"})

    assert [call[0] for call in transport.calls] == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert result["status"] == "ok"
    assert result["text"] == "Evidence\nBody text"
    assert "alert" not in result["text"]
    assert result["truncated"] is False


async def test_fetch_url_caps_text_and_refuses_unsupported_content() -> None:
    """Text is capped at the caller's limit; PDFs and errors are explicit envelopes."""
    long_html = "<p>" + "word " * 2_000 + "</p>"

    def responder(url: str) -> HTTPResponse:
        if url.endswith("/paper.pdf"):
            return http_response("%PDF-1.7", content_type="application/pdf", url=url)
        if url.endswith("/missing"):
            return http_response("x" * 2_000, status_code=500, content_type="text/html", url=url)
        return http_response(long_html, content_type="text/html", url=url)

    tool = FetchUrlTool(FakeHTTPClient(responder), FakeResolver())

    page = await tool.run({"url": "https://example.com/long", "max_chars": 500})
    assert page["status"] == "ok"
    assert len(page["text"]) == 500
    assert page["truncated"] is True
    assert page["source_chars"] > 500

    pdf = await tool.run({"url": "https://example.com/paper.pdf"})
    assert pdf["status"] == "unsupported_content_type"

    error = await tool.run({"url": "https://example.com/missing"})
    assert error["status"] == "http_error"
    assert error["http_status"] == 500
    assert len(error["text"]) <= 500

    with pytest.raises(ToolArgumentError, match="between 500 and 40000"):
        await tool.run({"url": "https://example.com/", "max_chars": 10})


def test_html_to_text_drops_scripts_decodes_entities_and_reports_truncation() -> None:
    """The parser is deterministic and never leaks script/style content into a prompt."""
    html = (
        "<html><head><style>p{color:red}</style><script>steal()</script></head>"
        "<body><h1>Title &amp; subtitle</h1><p>First&nbsp;line</p><ul><li>one</li>"
        "<li>two</li></ul></body></html>"
    )

    text, truncated = html_to_text(html, max_chars=10_000)
    assert text == "Title & subtitle\nFirst line\none\ntwo"
    assert truncated is False
    assert "steal" not in text
    assert "color:red" not in text

    cut, was_cut = html_to_text("<p>" + "a" * 50 + "</p>", max_chars=20)
    assert was_cut is True
    assert cut == "a" * 20


# --------------------------------------------------------------------------- lookup


async def test_lookup_source_by_id_and_normalised_doi() -> None:
    """A stored card comes back compact, by ULID or by normalised DOI."""
    card = sample_card()
    lookup = FakeSourceLookup([card])
    tool = LookupSourceTool(lookup)

    by_id = await tool.run({"source_id": card.id})
    assert by_id["status"] == "ok"
    assert by_id["source"]["id"] == card.id
    assert by_id["source"]["snippet"] == card.snippet
    assert by_id["source"]["verification"] == "verified"
    assert "schema_version" not in by_id["source"]

    by_doi = await tool.run({"doi": "https://doi.org/10.1234/JANS.2024.00042"})
    assert by_doi["status"] == "ok"
    assert lookup.doi_requests == ["10.1234/jans.2024.00042"]

    assert (await tool.run({"doi": "10.9999/nope"}))["status"] == "not_found"
    with pytest.raises(ToolArgumentError, match="exactly one"):
        await tool.run({})
    with pytest.raises(ToolArgumentError, match="exactly one"):
        await tool.run({"source_id": card.id, "doi": card.doi or ""})
    with pytest.raises(ToolArgumentError, match="ULID"):
        await tool.run({"source_id": "not-a-ulid"})
    with pytest.raises(ToolArgumentError, match="unknown argument"):
        await tool.run({"source_id": card.id, "limit": 1})


# --------------------------------------------------------------------------- registry


async def test_registry_wraps_tool_output_as_untrusted_and_redacts_secrets() -> None:
    """Execution goes through the capability, so untrusted framing, caps and redaction apply."""
    secret_shaped = "sk-ant-" + "a1B2" * 10
    card = sample_card(snippet=f"An accidental value {secret_shaped} in a source.")
    lookup = FakeSourceLookup([card])
    registry = build_tool_registry(
        http_client=FakeHTTPClient(lambda url: http_response({})),
        resolver=FakeResolver(),
        source_lookup=lookup,
        max_result_bytes=4_096,
    )
    access = registry.for_role(AgentRole.WRITER.value)

    result = await access.run("lookup_source", {"source_id": card.id})

    assert result.untrusted is True
    assert result.truncated is False
    assert "[REDACTED]" in result.data
    assert find_secrets(result.data) == []
    assert "UNTRUSTED TOOL DATA" in result.as_prompt_text()

    registry.disable_for_project("project-a", "lookup_source")
    with pytest.raises(ToolDisabledError):
        await registry.run(
            "lookup_source",
            {"source_id": card.id},
            role=AgentRole.WRITER,
            project_id="project-a",
        )


# --------------------------------------------------------------------------- api


def test_tools_endpoint_exposes_catalog_and_allowlist_without_secrets(settings: Settings) -> None:
    """The console can explain tool policy before a run exists."""
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/tools")

    assert response.status_code == 200
    body = response.json()
    assert {tool["name"] for tool in body["tools"]} == set(TOOL_NAMES)
    assert body["role_allowlist"]["architect"] == []
    assert body["role_allowlist"]["researcher"] == sorted(
        DEFAULT_ALLOWED_TOOLS_BY_ROLE[AgentRole.RESEARCHER]
    )
    assert find_secrets(response.text) == []


# --------------------------------------------------------------------------- httpx adapter


async def test_httpx_adapter_caps_bytes_and_never_follows_redirects() -> None:
    """The vendor adapter normalises a response and leaves redirect policy to the tool."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"location": "https://example.com/final", "content-type": "text/html"},
                content=b"",
            )
        return httpx.Response(
            200,
            headers={"content-type": "Application/JSON; charset=utf-8"},
            content=b"0123456789abcdef",
        )

    client = HttpxClient(
        max_bytes=10,
        user_agent="ScholarLoop-test",
        transport=httpx.MockTransport(handler),
    )

    body = await client.get("https://example.com/data")
    assert body.status_code == 200
    assert body.text == "0123456789"
    assert body.truncated is True
    assert body.content_type == "application/json"
    assert body.final_url == "https://example.com/data"

    redirect = await client.get("https://example.com/redirect")
    assert redirect.status_code == 302
    assert redirect.location == "https://example.com/final"
    assert redirect.final_url == "https://example.com/redirect"


async def test_httpx_adapter_maps_transport_and_timeout_errors() -> None:
    """A dead socket and a slow server are different, machine-readable reasons."""

    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def timeout_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(HTTPFetchError, match="transport_error"):
        await HttpxClient(transport=httpx.MockTransport(transport_error)).get(
            "https://example.com/"
        )
    with pytest.raises(HTTPFetchError, match="timeout"):
        await HttpxClient(transport=httpx.MockTransport(timeout_error)).get("https://example.com/")
