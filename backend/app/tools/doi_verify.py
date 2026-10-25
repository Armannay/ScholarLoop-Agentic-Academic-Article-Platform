"""Crossref DOI metadata comparison for validator-owned source verification."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from app.contracts.enums import VerificationStatus
from app.contracts.source import SourceCard
from app.tools.base import ToolContext, ToolResult
from app.tools.sanitize import sanitize_untrusted_text

__all__ = ["DoiVerifyTool"]

_SOURCE_PROPERTIES: dict[str, Any] = {
    "schema_version": {"type": "integer", "enum": [1]},
    "id": {"type": "string", "minLength": 26, "maxLength": 26},
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "authors": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
    "year": {"type": "integer", "minimum": 1400, "maximum": 2200},
    "venue": {"type": "string", "minLength": 1, "maxLength": 300},
    "doi": {"type": ["string", "null"], "maxLength": 255},
    "url": {"type": ["string", "null"], "maxLength": 2_000},
    "snippet": {"type": "string", "maxLength": 8_000},
    "verification": {"type": "string", "enum": ["unknown", "verified", "mismatch"]},
}


def _normal_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", normalized).split())


def _publication_year(message: Mapping[str, Any]) -> int | None:
    for field in ("published-print", "published-online", "issued", "created"):
        date = message.get(field)
        parts = date.get("date-parts") if isinstance(date, Mapping) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int) and 1400 <= year <= 2200:
                return year
    return None


class DoiVerifyTool:
    """Compare a supplied SourceCard with Crossref; never upgrades without metadata."""

    _NAME: ClassVar[str] = "doi_verify"
    _DESCRIPTION: ClassVar[str] = (
        "Compare a source card's title and year with Crossref DOI metadata; unknown stays unknown."
    )
    _TOKEN_COST: ClassVar[int] = 12
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "object",
                "properties": _SOURCE_PROPERTIES,
                "required": ["id", "title", "year", "venue", "snippet"],
                "additionalProperties": False,
            }
        },
        "required": ["source"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 6.0,
    ) -> None:
        self.name = self._NAME
        self.description = self._DESCRIPTION
        self.token_cost = self._TOKEN_COST
        self.parameters = dict(self._PARAMETERS)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        try:
            source = SourceCard.model_validate(arguments["source"])
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult(tool=self.name, ok=False, content=f"invalid SourceCard: {exc}")
        if source.doi is None:
            return ToolResult(tool=self.name, ok=False, content="source has no DOI to verify")

        # VERIFY: Crossref's single-work endpoint and response field shape should be
        # checked against the service's current REST documentation when upgrading.
        url = f"https://api.crossref.org/works/{quote(source.doi, safe='/')}"
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.get(url, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                content=json.dumps(
                    {"error": "Crossref metadata unavailable", "reason": type(exc).__name__},
                    separators=(",", ":"),
                ),
            )

        message = payload.get("message") if isinstance(payload, Mapping) else None
        if not isinstance(message, Mapping):
            return self._result(source, source.verification, [], metadata_available=False)

        titles = message.get("title")
        metadata_title = titles[0] if isinstance(titles, list) and titles else None
        metadata_year = _publication_year(message)
        comparisons: list[dict[str, Any]] = []
        mismatches = False
        if isinstance(metadata_title, str) and metadata_title.strip():
            clean_title = sanitize_untrusted_text(metadata_title, max_chars=500).text
            title_matches = _normal_title(source.title) == _normal_title(clean_title)
            comparisons.append({"field": "title", "matches": title_matches})
            mismatches = mismatches or not title_matches
        if metadata_year is not None:
            year_matches = source.year == metadata_year
            comparisons.append(
                {"field": "year", "matches": year_matches, "metadata_year": metadata_year}
            )
            mismatches = mismatches or not year_matches

        if not comparisons:
            status = source.verification
        elif mismatches:
            status = VerificationStatus.MISMATCH
        else:
            status = VerificationStatus.VERIFIED
        return self._result(source, status, comparisons, metadata_available=bool(comparisons))

    def _result(
        self,
        source: SourceCard,
        status: VerificationStatus,
        comparisons: list[dict[str, Any]],
        *,
        metadata_available: bool,
    ) -> ToolResult:
        updated = source.model_copy(update={"verification": status})
        payload = {
            "updated_source": {
                "id": updated.id,
                "title": updated.title,
                "year": updated.year,
                "doi": updated.doi,
                "verification": status.value,
            },
            "metadata_compared": metadata_available,
            "comparisons": comparisons,
        }
        return ToolResult(
            tool=self.name,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    async def aclose(self) -> None:
        """Close the internally created HTTP client only."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
