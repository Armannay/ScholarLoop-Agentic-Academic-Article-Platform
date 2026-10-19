"""Crossref metadata tools: resolve a DOI, search bibliographic metadata.

Why Crossref: it answers with *publisher-deposited metadata*, so the tool can copy
title, authors, year, venue and URL instead of asking a model to remember them
(HARD RULE 1). A field Crossref does not supply stays ``null`` and is listed in
``missing``; the tool never fills a gap with a guess.

``# VERIFY`` (third-party API, isolated here): the REST paths ``/works/{doi}`` and
``/works?query.bibliographic=``, the ``select`` field list, the ``mailto`` polite-pool
parameter and the response keys (``message``, ``title[]``, ``author[].family/given``,
``issued.date-parts``, ``container-title[]``, ``publisher``, ``URL``). Re-check these
against the live API before the first production run; the unit tests use recorded-
shape fixtures and never call the network (HARD RULE 5).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from app.contracts.source import normalise_doi
from app.tools.arguments import optional_int, reject_unknown_arguments, require_string
from app.tools.errors import HTTPFetchError, ToolArgumentError
from app.tools.http import DEFAULT_TIMEOUT_S, HTTPClient

__all__ = [
    "CROSSREF_BASE_URL",
    "CrossrefClient",
    "CrossrefDoiTool",
    "CrossrefSearchTool",
    "normalise_doi_argument",
]

CROSSREF_BASE_URL: Final[str] = "https://api.crossref.org"

#: Keep the search payload small: only fields a SourceCard can use.
_SELECT_FIELDS: Final[str] = "DOI,title,author,issued,container-title,publisher,type,URL"

_DOI_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"^10\.\d{4,9}/\S+$")

#: Authors beyond this are summarised as a count, not pasted into the prompt.
MAX_AUTHORS: Final[int] = 20
MAX_SEARCH_LIMIT: Final[int] = 10
MAX_SNIPPET_TITLE: Final[int] = 500


def normalise_doi_argument(value: object) -> str:
    """Validate a DOI argument and return its canonical lower-case form."""
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError("doi must be a non-empty string")
    cleaned = normalise_doi(value)
    if len(cleaned) > 255 or not _DOI_SHAPE_RE.match(cleaned):
        raise ToolArgumentError("doi must look like 10.xxxx/suffix")
    return cleaned


def _json_object(text: str) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _first_text(value: object) -> str | None:
    """First non-blank string in a scalar-or-list Crossref field."""
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _year_from(value: object) -> int | None:
    """Extract a plausible publication year from a Crossref date object."""
    if not isinstance(value, Mapping):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return None
    first = parts[0]
    if not isinstance(first, list) or not first:
        return None
    year = first[0]
    if isinstance(year, bool) or not isinstance(year, int):
        return None
    return year if 1400 <= year <= 2200 else None


def _publication_year(work: Mapping[str, Any]) -> int | None:
    for key in ("issued", "published-print", "published-online"):
        year = _year_from(work.get(key))
        if year is not None:
            return year
    return None


def _authors(work: Mapping[str, Any]) -> list[str]:
    """Family-name-first author labels, capped to keep prompts bounded."""
    raw = work.get("author")
    if not isinstance(raw, list):
        return []
    authors: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        family = item.get("family")
        given = item.get("given")
        name = item.get("name")
        label: str | None = None
        if isinstance(family, str) and family.strip():
            label = family.strip()
            if isinstance(given, str) and given.strip():
                label = f"{label}, {given.strip()}"
        elif isinstance(name, str) and name.strip():
            label = name.strip()
        if label is None:
            continue
        authors.append(label)
        if len(authors) >= MAX_AUTHORS:
            break
    return authors


def _http_url(value: object) -> str | None:
    text = _first_text(value)
    if text is None or not text.startswith(("http://", "https://")):
        return None
    return text


def summarise_work(work: Mapping[str, Any], *, fallback_doi: str | None = None) -> dict[str, Any]:
    """Map a Crossref ``message`` onto the fields a SourceCard can reuse.

    ``missing`` lists the fields the response did not supply. ``complete`` is true only
    when title, year and venue are all present; completeness is a fact about the
    metadata, not a judgement about the source.
    """
    response_doi = normalise_doi(_first_text(work.get("DOI")) or "")
    doi = response_doi if _DOI_SHAPE_RE.match(response_doi) else fallback_doi
    title = _first_text(work.get("title"))
    venue = _first_text(work.get("container-title")) or _first_text(work.get("publisher"))
    year = _publication_year(work)
    url = _http_url(work.get("URL"))
    authors = _authors(work)

    missing = [
        name
        for name, present in (("title", title), ("year", year), ("venue", venue))
        if present is None
    ]
    return {
        "doi": doi,
        "title": title[:MAX_SNIPPET_TITLE] if title else None,
        "authors": authors,
        "year": year,
        "venue": venue,
        "url": url,
        "type": _first_text(work.get("type")),
        "publisher": _first_text(work.get("publisher")),
        "locatable": doi is not None or url is not None,
        "complete": not missing,
        "missing": missing,
    }


class CrossrefClient:
    """Thin adapter over the Crossref REST API; all vendor detail lives here."""

    def __init__(
        self,
        http: HTTPClient,
        *,
        base_url: str = CROSSREF_BASE_URL,
        mailto: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._mailto = mailto.strip() if mailto and mailto.strip() else None
        self._timeout_s = timeout_s

    def _params(self, **extra: str) -> dict[str, str] | None:
        params = dict(extra)
        if self._mailto is not None:
            params["mailto"] = self._mailto
        return params or None

    async def lookup_doi(self, doi: str) -> dict[str, Any]:
        """Resolve one DOI to metadata, or report exactly why it could not."""
        url = f"{self._base_url}/works/{quote(doi, safe='/')}"
        try:
            response = await self._http.get(
                url,
                params=self._params(),
                headers={"accept": "application/json"},
                timeout_s=self._timeout_s,
            )
        except HTTPFetchError as exc:
            return {"status": "unavailable", **exc.as_dict()}

        if response.status_code == 404:
            return {"status": "not_found", "doi": doi}
        if not 200 <= response.status_code < 300:
            return {
                "status": "unavailable",
                "reason": "http_error",
                "http_status": response.status_code,
            }
        payload = _json_object(response.text)
        work = payload.get("message") if payload is not None else None
        if not isinstance(work, Mapping):
            return {"status": "unavailable", "reason": "malformed_response"}
        return {"status": "ok", **summarise_work(work, fallback_doi=doi)}

    async def search(self, query: str, *, limit: int) -> dict[str, Any]:
        """Search bibliographic metadata. Zero hits is a valid, empty answer."""
        try:
            response = await self._http.get(
                f"{self._base_url}/works",
                params=self._params(
                    **{
                        "query.bibliographic": query,
                        "rows": str(limit),
                        "select": _SELECT_FIELDS,
                    }
                ),
                headers={"accept": "application/json"},
                timeout_s=self._timeout_s,
            )
        except HTTPFetchError as exc:
            return {"status": "unavailable", "query": query, **exc.as_dict()}

        if not 200 <= response.status_code < 300:
            return {
                "status": "unavailable",
                "reason": "http_error",
                "http_status": response.status_code,
                "query": query,
            }
        payload = _json_object(response.text)
        message = payload.get("message") if payload is not None else None
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            return {"status": "unavailable", "reason": "malformed_response", "query": query}

        results = [summarise_work(item) for item in items[:limit] if isinstance(item, Mapping)]
        return {
            "status": "ok" if results else "not_found",
            "query": query,
            "count": len(results),
            "results": results,
        }


class CrossrefDoiTool:
    """``crossref_doi``: one DOI in, copied metadata or an explicit miss out."""

    name = "crossref_doi"
    token_cost_hint = 400
    parameters: Final[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "minLength": 7,
                "maxLength": 255,
                "description": "DOI, with or without a https://doi.org/ prefix.",
            }
        },
        "required": ["doi"],
        "additionalProperties": False,
    }

    def __init__(self, client: CrossrefClient) -> None:
        self._client = client

    async def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reject_unknown_arguments(arguments, self.parameters["properties"])
        doi = normalise_doi_argument(arguments.get("doi"))
        return await self._client.lookup_doi(doi)


class CrossrefSearchTool:
    """``search_sources``: bounded bibliographic search, metadata only."""

    name = "search_sources"
    token_cost_hint = 900
    parameters: Final[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 300,
                "description": "Bibliographic query: title words, authors, topic.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_LIMIT,
                "default": 5,
                "description": "Maximum number of candidates to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, client: CrossrefClient) -> None:
        self._client = client

    async def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reject_unknown_arguments(arguments, self.parameters["properties"])
        query = require_string(arguments, "query", max_length=300)
        if len(query) < 2:
            raise ToolArgumentError("query must be at least 2 characters")
        limit = optional_int(
            arguments,
            "limit",
            default=5,
            minimum=1,
            maximum=MAX_SEARCH_LIMIT,
        )
        return await self._client.search(query, limit=limit)
