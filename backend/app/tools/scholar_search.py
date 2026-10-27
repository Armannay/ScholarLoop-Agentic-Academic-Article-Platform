"""Cache-first literature discovery using OpenAlex and two metadata fallbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx

from app.contracts.source import SourceCard, normalise_doi
from app.core.ids import new_ulid
from app.tools.base import ToolContext, ToolResult
from app.tools.sanitize import sanitize_untrusted_text

__all__ = ["CACHE_TTL_SECONDS", "SQLiteQueryCache", "ScholarSearchTool"]

CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_SEARCH_RESULTS = 20
_MAX_RETRY_WAIT_SECONDS = 5.0
_DEFAULT_CACHE_PATH = Path.home() / ".cache" / "scholarloop" / "research.sqlite3"
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")


def _clean_words(value: str, *, limit: int = 60) -> str:
    return " ".join(value.split()[:limit])


def _first_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        return next(
            (item.strip() for item in value if isinstance(item, str) and item.strip()), None
        )
    return None


def _year(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1400 <= value <= 2200:
        return value
    if isinstance(value, list) and value:
        return _year(value[0])
    if isinstance(value, str):
        match = re.search(r"\b(1[4-9]\d{2}|20\d{2}|21\d{2}|2200)\b", value)
        if match:
            return int(match.group(1))
    return None


def _safe_url(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 2_000
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return None
    except ValueError:
        return None
    return value


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
    """Normalize index metadata without inventing a year, venue, or locator."""
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
                    family, given = author.get("family"), author.get("given")
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
    doi_value = normalise_doi(doi) if isinstance(doi, str) and doi.strip() else None
    if doi_value is not None and (len(doi_value) > 255 or not _DOI_SHAPE.fullmatch(doi_value)):
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
        # A title is real metadata, not an invented abstract. Label it explicitly.
        clean_snippet = _clean_words(f"Title: {clean_title}", limit=30)
    identity = f"doi:{doi_value}" if doi_value else f"url:{clean_url}"
    # A stable, valid ULID: epoch prefix denotes a content-derived rather than
    # creation-time ID; 80 digest bits are used for the identity component.
    source_id = new_ulid(timestamp_ms=0, entropy=hashlib.sha256(identity.encode()).digest()[:10])
    try:
        return SourceCard(
            id=source_id,
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
        host = record.get("host_venue")
        venue = host.get("display_name") if isinstance(host, Mapping) else None
    authorships = record.get("authorships", [])
    authors: list[Any] = []
    if isinstance(authorships, list):
        for authorship in authorships:
            if isinstance(authorship, Mapping) and isinstance(authorship.get("author"), Mapping):
                authors.append(authorship["author"])
    abstract = ""
    inverted = record.get("abstract_inverted_index")
    if isinstance(inverted, Mapping):
        positioned: dict[int, str] = {}
        for word, positions in inverted.items():
            if isinstance(word, str) and isinstance(positions, list):
                for position in positions:
                    if (
                        isinstance(position, int)
                        and not isinstance(position, bool)
                        and 0 <= position < 400
                    ):
                        previous = positioned.get(position)
                        if previous is None or word < previous:
                            positioned[position] = word
        abstract = " ".join(positioned[position] for position in sorted(positioned))
    return _make_card(
        title=record.get("title") or record.get("display_name"),
        authors=authors,
        year=_year(record.get("publication_year")) or _year(record.get("publication_date")),
        venue=venue,
        doi=record.get("doi"),
        url=(location.get("landing_page_url") if isinstance(location, Mapping) else None)
        or record.get("id"),
        snippet=abstract,
    )


def _crossref_card(record: Mapping[str, Any]) -> SourceCard | None:
    year = None
    for field in ("published", "issued", "published-print", "published-online"):
        date = record.get(field)
        parts = date.get("date-parts") if isinstance(date, Mapping) else None
        year = _year(parts)
        if year is not None:
            break
    return _make_card(
        title=_first_text(record.get("title")),
        authors=record.get("author"),
        year=year,
        venue=_first_text(record.get("container-title")) or record.get("publisher"),
        doi=record.get("DOI"),
        url=record.get("URL"),
        snippet=_first_text(record.get("abstract")),
    )


def _semantic_scholar_card(record: Mapping[str, Any]) -> SourceCard | None:
    external_ids = record.get("externalIds")
    doi = external_ids.get("DOI") if isinstance(external_ids, Mapping) else None
    journal = record.get("journal")
    venue = record.get("venue") or (journal.get("name") if isinstance(journal, Mapping) else None)
    return _make_card(
        title=record.get("title"),
        authors=record.get("authors"),
        year=record.get("year"),
        venue=venue,
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
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT created_at, payload FROM search_cache WHERE query_hash = ?", (query_hash,)
            ).fetchone()
            if row is None:
                return None
            created_at, payload = row
            if not 0 <= now - float(created_at) < self.ttl_seconds:
                connection.execute("DELETE FROM search_cache WHERE query_hash = ?", (query_hash,))
                return None
            try:
                parsed = json.loads(str(payload))
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, list) else None

    def _put(self, query_hash: str, payload: list[dict[str, Any]], now: float) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO search_cache(query_hash, created_at, payload) "
                "VALUES (?, ?, ?)",
                (query_hash, now, encoded),
            )

    async def get(self, query_hash: str) -> list[dict[str, Any]] | None:
        return await asyncio.to_thread(self._get, query_hash, time.time())

    async def put(self, query_hash: str, payload: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._put, query_hash, payload, time.time())


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _title_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


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
        min_interval_seconds: float = 1.0,
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
        self._search_lock = asyncio.Lock()
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
                if response.status_code == 429 or response.status_code >= 500:
                    wait = _retry_after(response)
                    if wait is None:
                        wait = self._backoff * (2**attempt)
                    # Never retry *earlier* than the server requested. Fall back to
                    # the next index instead if its requested delay is too long.
                    if attempt + 1 < self._attempts and wait <= _MAX_RETRY_WAIT_SECONDS:
                        await asyncio.sleep(wait)
                        continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                if attempt + 1 >= self._attempts:
                    raise RuntimeError(
                        f"research index request failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(self._backoff * (2**attempt))
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
        # VERIFY: OpenAlex endpoint, date-filter syntax, and envelope may evolve.
        payload = await self._request_json("https://api.openalex.org/works", params)
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise RuntimeError("invalid OpenAlex response envelope")
        return [
            card
            for record in results[:limit]
            if isinstance(record, Mapping) and (card := _openalex_card(record)) is not None
        ]

    async def _crossref(
        self, query: str, limit: int, year_from: int | None, year_to: int | None
    ) -> list[SourceCard]:
        params: dict[str, Any] = {"query": query, "rows": limit}
        filters: list[str] = []
        if year_from is not None:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"until-pub-date:{year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        # VERIFY: Crossref query and publication-date filter parameters may evolve.
        payload = await self._request_json("https://api.crossref.org/works", params)
        message = payload.get("message") if isinstance(payload, Mapping) else None
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            raise RuntimeError("invalid Crossref response envelope")
        return [
            card
            for record in items[:limit]
            if isinstance(record, Mapping) and (card := _crossref_card(record)) is not None
        ]

    async def _semantic_scholar(
        self, query: str, limit: int, year_from: int | None, year_to: int | None
    ) -> list[SourceCard]:
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "fields": "title,authors,year,venue,journal,externalIds,url,abstract",
        }
        if year_from is not None or year_to is not None:
            params["year"] = f"{year_from or ''}-{year_to or ''}"
        # VERIFY: Semantic Scholar endpoint, fields, date-range syntax and quotas may change.
        payload = await self._request_json(
            "https://api.semanticscholar.org/graph/v1/paper/search", params
        )
        items = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise RuntimeError("invalid Semantic Scholar response envelope")
        return [
            card
            for record in items[:limit]
            if isinstance(record, Mapping) and (card := _semantic_scholar_card(record)) is not None
        ]

    @staticmethod
    def _deduplicate(cards: list[SourceCard], limit: int) -> list[SourceCard]:
        by_doi: set[str] = set()
        titles: list[str] = []
        output: list[SourceCard] = []
        for card in cards:
            title = _title_key(card.title)
            if card.doi is not None and card.doi in by_doi:
                continue
            if any(
                title == other
                or (
                    min(len(title), len(other)) >= 20
                    and SequenceMatcher(None, title, other, autojunk=False).ratio() >= 0.92
                )
                for other in titles
            ):
                continue
            if card.doi is not None:
                by_doi.add(card.doi)
            titles.append(title)
            output.append(card)
            if len(output) >= limit:
                break
        return output

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        query = " ".join(unicodedata.normalize("NFKC", str(arguments["query"])).casefold().split())
        if not query:
            return ToolResult(tool=self.name, ok=False, content="query must not be blank")
        limit = min(MAX_SEARCH_RESULTS, max(1, int(arguments.get("max_results", 10))))
        year_from = arguments.get("year_from")
        year_to = arguments.get("year_to")
        if year_from is not None and year_to is not None and int(year_from) > int(year_to):
            return ToolResult(tool=self.name, ok=False, content="year_from must not exceed year_to")
        # Fetch a fixed bounded batch, so changing N does not poison or fragment the
        # cache. Date filters are part of the identity; the HTTP query is normalized too.
        parameters = {"query": query, "year_from": year_from, "year_to": year_to}
        query_hash = hashlib.sha256(
            json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        async with self._search_lock:
            cached = await self.cache.get(query_hash)
            if cached is not None:
                try:
                    cards = [SourceCard.model_validate(item) for item in cached]
                except (ValueError, TypeError):
                    cards = None
                if cards is not None:
                    return self._result(cards[:limit], cache_hit=True)

            errors: list[str] = []
            successes = 0
            searchers: tuple[Callable[[], Awaitable[list[SourceCard]]], ...] = (
                lambda: self._openalex(query, MAX_SEARCH_RESULTS, year_from, year_to),
                lambda: self._crossref(query, MAX_SEARCH_RESULTS, year_from, year_to),
                lambda: self._semantic_scholar(query, MAX_SEARCH_RESULTS, year_from, year_to),
            )
            for search in searchers:
                try:
                    found = await search()
                    successes += 1
                    found = [
                        card
                        for card in found
                        if (year_from is None or card.year >= int(year_from))
                        and (year_to is None or card.year <= int(year_to))
                    ]
                    cards = self._deduplicate(found, MAX_SEARCH_RESULTS)
                    if cards:
                        await self.cache.put(
                            query_hash, [card.model_dump(mode="json") for card in cards]
                        )
                        return self._result(cards[:limit], cache_hit=False)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    errors.append(type(exc).__name__)
            if not errors:
                await self.cache.put(query_hash, [])
                return self._result([], cache_hit=False)
            message = (
                "all research indexes failed" if successes == 0 else "some research indexes failed"
            )
            return ToolResult(
                tool=self.name,
                ok=False,
                content=json.dumps({"error": message, "failures": errors}, separators=(",", ":")),
            )

    def _result(self, cards: list[SourceCard], *, cache_hit: bool) -> ToolResult:
        bounded_cards = list(cards)
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
            tool=self.name, content=content, truncated=len(bounded_cards) < len(cards)
        )

    async def aclose(self) -> None:
        """Close a client created by this tool; injected clients remain caller-owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
