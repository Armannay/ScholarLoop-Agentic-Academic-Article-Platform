"""Cache-first, deterministic literature discovery using OpenAlex and fallbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from app.contracts.source import SourceCard, normalise_doi
from app.core.ids import new_ulid
from app.tools.base import ToolContext, ToolResult
from app.tools.sanitize import sanitize_untrusted_text

__all__ = ["CACHE_TTL_SECONDS", "SQLiteQueryCache", "ScholarSearchTool"]

CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_SEARCH_RESULTS = 5
_DEFAULT_CACHE_PATH = Path.home() / ".cache" / "scholarloop" / "research.sqlite3"


def _clean_words(value: str, *, limit: int = 60) -> str:
    words = value.split()
    return " ".join(words[:limit])


def _first_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _year(value: Any) -> int | None:
    if isinstance(value, int) and 1400 <= value <= 2200:
        return value
    if isinstance(value, list) and value and isinstance(value[0], int):
        return _year(value[0])
    if isinstance(value, str):
        match = re.search(r"\b(1[4-9]\d{2}|20\d{2}|21\d{2}|2200)\b", value)
        if match:
            return int(match.group(1))
    return None


def _safe_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value[:2_000]
    return None


def _make_card(
    *,
    title: Any,
    authors: Any,
    year: Any,
    venue: Any,
    doi: Any,
    url: Any,
    snippet: Any,
) -> SourceCard | None:
    """Normalize only metadata actually supplied by an index; never fill unknowns."""
    if not isinstance(title, str) or not isinstance(venue, str):
        return None
    clean_title = sanitize_untrusted_text(title, max_chars=300).text
    clean_venue = sanitize_untrusted_text(venue, max_chars=200).text
    parsed_year = _year(year)
    if not clean_title or not clean_venue or parsed_year is None:
        return None
    clean_authors: list[str] = []
    if isinstance(authors, list):
        for author in authors:
            name: str | None = None
            if isinstance(author, str):
                name = author
            elif isinstance(author, Mapping):
                raw_name = author.get("display_name") or author.get("name")
                if isinstance(raw_name, str):
                    name = raw_name
                else:
                    family = author.get("family")
                    given = author.get("given")
                    if isinstance(family, str) and isinstance(given, str):
                        name = f"{family}, {given}"
                    elif isinstance(family, str):
                        name = family
            if name:
                cleaned = sanitize_untrusted_text(name, max_chars=80).text
                if cleaned:
                    clean_authors.append(cleaned)
            if len(clean_authors) == 5:
                break
    doi_value = doi.strip() if isinstance(doi, str) else None
    if doi_value:
        try:
            doi_value = normalise_doi(doi_value)
        except (AttributeError, TypeError):
            doi_value = None
        if doi_value is None or not doi_value.startswith("10."):
            doi_value = None
    clean_url = _safe_url(url)
    if clean_url is None and doi_value:
        clean_url = f"https://doi.org/{quote(doi_value, safe='/')}"
    if doi_value is None and clean_url is None:
        return None
    clean_snippet = ""
    if isinstance(snippet, str):
        clean_snippet = _clean_words(
            sanitize_untrusted_text(snippet, max_chars=2_000).text, limit=30
        )
    if not clean_snippet:
        return None
    try:
        return SourceCard(
            id=new_ulid(),
            title=clean_title,
            authors=clean_authors,
            year=parsed_year,
            venue=clean_venue,
            doi=doi_value,
            url=clean_url,
            snippet=clean_snippet,
        )
    except ValueError:
        return None


def _openalex_card(record: Mapping[str, Any]) -> SourceCard | None:
    location = record.get("primary_location")
    source = location.get("source") if isinstance(location, Mapping) else None
    venue = source.get("display_name") if isinstance(source, Mapping) else None
    if not venue:
        venue = record.get("host_venue")
        if isinstance(venue, Mapping):
            venue = venue.get("display_name")
    authorships = record.get("authorships", [])
    authors: list[Any] = []
    if isinstance(authorships, list):
        for authorship in authorships:
            if isinstance(authorship, Mapping):
                author = authorship.get("author")
                if isinstance(author, Mapping):
                    authors.append(author)
    abstract = ""
    inverted = record.get("abstract_inverted_index")
    if isinstance(inverted, Mapping):
        positioned: list[tuple[int, str]] = []
        for word, positions in inverted.items():
            if isinstance(word, str) and isinstance(positions, list):
                positioned.extend(
                    (position, word) for position in positions if isinstance(position, int)
                )
        abstract = " ".join(word for _, word in sorted(positioned)[:400])
    return _make_card(
        title=record.get("title"),
        authors=authors,
        year=record.get("publication_year"),
        venue=venue,
        doi=record.get("doi"),
        url=(location.get("landing_page_url") if isinstance(location, Mapping) else None)
        or record.get("id"),
        snippet=abstract,
    )


def _crossref_card(record: Mapping[str, Any]) -> SourceCard | None:
    published = (
        record.get("published-print") or record.get("published-online") or record.get("created")
    )
    date_parts = published.get("date-parts") if isinstance(published, Mapping) else None
    return _make_card(
        title=_first_text(record.get("title")),
        authors=record.get("author"),
        year=date_parts[0] if isinstance(date_parts, list) and date_parts else None,
        venue=_first_text(record.get("container-title")) or record.get("publisher"),
        doi=record.get("DOI"),
        url=record.get("URL"),
        snippet=_first_text(record.get("abstract")),
    )


def _semantic_scholar_card(record: Mapping[str, Any]) -> SourceCard | None:
    external_ids = record.get("externalIds")
    doi = external_ids.get("DOI") if isinstance(external_ids, Mapping) else None
    return _make_card(
        title=record.get("title"),
        authors=record.get("authors"),
        year=record.get("year"),
        venue=record.get("venue"),
        doi=doi,
        url=record.get("url"),
        snippet=record.get("abstract"),
    )


class SQLiteQueryCache:
    """SQLite cache keyed by a stable query hash and bounded by a fixed TTL."""

    def __init__(self, path: Path, *, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds cannot be negative")
        self.path = path
        self.ttl_seconds = ttl_seconds

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS search_cache "
            "(query_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        return connection

    def _get(self, query_hash: str, now: float) -> list[dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at, payload FROM search_cache WHERE query_hash = ?",
                (query_hash,),
            ).fetchone()
            if row is None:
                return None
            created_at, payload = row
            if now - float(created_at) >= self.ttl_seconds:
                connection.execute("DELETE FROM search_cache WHERE query_hash = ?", (query_hash,))
                return None
            parsed = json.loads(str(payload))
            return parsed if isinstance(parsed, list) else None

    def _put(self, query_hash: str, payload: list[dict[str, Any]], now: float) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO search_cache(query_hash, created_at, payload) "
                "VALUES (?, ?, ?)",
                (query_hash, now, encoded),
            )

    async def get(self, query_hash: str) -> list[dict[str, Any]] | None:
        return await asyncio.to_thread(self._get, query_hash, time.time())

    async def put(self, query_hash: str, payload: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._put, query_hash, payload, time.time())


class ScholarSearchTool:
    """Search OpenAlex first, then Crossref and Semantic Scholar as fallbacks."""

    _NAME: ClassVar[str] = "scholar_search"
    _DESCRIPTION: ClassVar[str] = (
        "Find locatable scholarly works via OpenAlex, with Crossref and Semantic Scholar fallbacks."
    )
    _TOKEN_COST: ClassVar[int] = 20
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
            "year_from": {"type": "integer", "minimum": 1400, "maximum": 2200},
            "year_to": {"type": "integer", "minimum": 1400, "maximum": 2200},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 6.0,
        attempts: int = 2,
        backoff_seconds: float = 0.15,
        min_interval_seconds: float = 0.1,
    ) -> None:
        self.name = self._NAME
        self.description = self._DESCRIPTION
        self.token_cost = self._TOKEN_COST
        self.parameters = dict(self._PARAMETERS)
        self.cache = SQLiteQueryCache(cache_path or _DEFAULT_CACHE_PATH)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._attempts = max(1, min(attempts, 3))
        self._backoff = max(0.0, min(backoff_seconds, 2.0))
        self._min_interval = max(0.0, min(min_interval_seconds, 2.0))
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": "ScholarLoop/0.1 (polite academic metadata lookup)"},
            )
        return self._client

    async def _request_json(self, url: str, params: Mapping[str, Any]) -> Any:
        client = await self._http()
        for attempt in range(self._attempts):
            async with self._request_lock:
                delay = self._min_interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request = time.monotonic()
            try:
                response = await client.get(url, params=dict(params), timeout=self._timeout)
                # VERIFY: index response envelopes and query parameter names are maintained
                # by the respective services; adapters are isolated in this module.
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < self._attempts:
                        await asyncio.sleep(self._backoff * (attempt + 1))
                        continue
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                if attempt + 1 >= self._attempts:
                    raise RuntimeError(
                        f"research index request failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(self._backoff * (attempt + 1))
        raise RuntimeError("research index request failed after bounded retries")

    async def _openalex(
        self, query: str, limit: int, year_from: int | None, year_to: int | None
    ) -> list[SourceCard]:
        filters: list[str] = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to}-12-31")
        params: dict[str, Any] = {"search": query, "per-page": limit}
        if filters:
            params["filter"] = ",".join(filters)
        payload = await self._request_json("https://api.openalex.org/works", params)
        results = payload.get("results", []) if isinstance(payload, Mapping) else []
        return (
            [
                card
                for record in results[:limit]
                if isinstance(record, Mapping) and (card := _openalex_card(record)) is not None
            ]
            if isinstance(results, list)
            else []
        )

    async def _crossref(self, query: str, limit: int) -> list[SourceCard]:
        payload = await self._request_json(
            "https://api.crossref.org/works", {"query": query, "rows": limit}
        )
        message = payload.get("message", {}) if isinstance(payload, Mapping) else {}
        items = message.get("items", []) if isinstance(message, Mapping) else []
        return (
            [
                card
                for record in items[:limit]
                if isinstance(record, Mapping) and (card := _crossref_card(record)) is not None
            ]
            if isinstance(items, list)
            else []
        )

    async def _semantic_scholar(self, query: str, limit: int) -> list[SourceCard]:
        # VERIFY: Semantic Scholar may impose API-key-specific quotas and field changes.
        payload = await self._request_json(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            {
                "query": query,
                "limit": limit,
                "fields": "title,authors,year,venue,externalIds,url,abstract",
            },
        )
        items = payload.get("data", []) if isinstance(payload, Mapping) else []
        return (
            [
                card
                for record in items[:limit]
                if isinstance(record, Mapping)
                and (card := _semantic_scholar_card(record)) is not None
            ]
            if isinstance(items, list)
            else []
        )

    @staticmethod
    def _deduplicate(cards: list[SourceCard], limit: int) -> list[SourceCard]:
        by_doi: set[str] = set()
        by_title: set[str] = set()
        output: list[SourceCard] = []
        for card in cards:
            title_key = re.sub(r"[^\w]+", " ", card.title.casefold()).strip()
            if card.doi is not None and card.doi in by_doi:
                continue
            if title_key in by_title:
                continue
            if card.doi is not None:
                by_doi.add(card.doi)
            by_title.add(title_key)
            output.append(card)
            if len(output) >= limit:
                break
        return output

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        query = str(arguments["query"]).strip()
        limit = min(MAX_SEARCH_RESULTS, max(1, int(arguments.get("max_results", 10))))
        year_from = arguments.get("year_from")
        year_to = arguments.get("year_to")
        if year_from is not None and year_to is not None and int(year_from) > int(year_to):
            return ToolResult(tool=self.name, ok=False, content="year_from must not exceed year_to")
        parameters = {
            "query": " ".join(query.casefold().split()),
            "max_results": limit,
            "year_from": year_from,
            "year_to": year_to,
        }
        query_hash = hashlib.sha256(
            json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cached = await self.cache.get(query_hash)
        if cached is not None:
            cards = [SourceCard.model_validate(item) for item in cached]
            return self._result(cards, cache_hit=True)

        collected: list[SourceCard] = []
        errors: list[str] = []
        searchers: tuple[Callable[[], Awaitable[list[SourceCard]]], ...] = (
            lambda: self._openalex(query, limit, year_from, year_to),
            lambda: self._crossref(query, limit),
            lambda: self._semantic_scholar(query, limit),
        )
        for search in searchers:
            try:
                found = await search()
                collected.extend(found)
                if found:
                    break
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        cards = self._deduplicate(collected, limit)
        if cards or not errors:
            await self.cache.put(query_hash, [card.model_dump(mode="json") for card in cards])
            return self._result(cards, cache_hit=False)
        return ToolResult(
            tool=self.name,
            ok=False,
            content=json.dumps(
                {"error": "all research indexes failed", "failures": errors},
                separators=(",", ":"),
            ),
        )

    def _result(self, cards: list[SourceCard], *, cache_hit: bool) -> ToolResult:
        bounded_cards = list(cards)
        content = ""
        while True:
            payload = {
                "cache_hit": cache_hit,
                "count": len(bounded_cards),
                "result_limited": len(bounded_cards) < len(cards),
                "sources": [card.model_dump(mode="json") for card in bounded_cards],
            }
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content) <= 7_500 or not bounded_cards:
                break
            bounded_cards.pop()
        return ToolResult(
            tool=self.name,
            content=content,
            truncated=len(bounded_cards) < len(cards),
        )

    async def aclose(self) -> None:
        """Close a client created by this tool; injected clients remain caller-owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
