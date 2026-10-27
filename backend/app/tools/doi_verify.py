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
from app.contracts.source import SourceCard, normalise_doi
from app.tools.base import ToolContext, ToolResult
from app.tools.sanitize import sanitize_untrusted_text

__all__ = ["DoiVerifyTool"]

_SOURCE_PROPERTIES: dict[str, Any] = {
    "schema_version": {"type": "integer", "enum": [1]},
    "id": {"type": "string", "minLength": 26, "maxLength": 26},
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "authors": {
        "type": "array",
        "items": {"type": "string", "maxLength": 500},
        "maxItems": 200,
    },
    "year": {"type": "integer", "minimum": 1400, "maximum": 2200},
    "venue": {"type": "string", "minLength": 1, "maxLength": 300},
    "doi": {"type": ["string", "null"], "maxLength": 255},
    "url": {"type": ["string", "null"], "maxLength": 2_000},
    "snippet": {"type": "string", "maxLength": 8_000},
    "verification": {"type": "string", "enum": ["unknown", "verified", "mismatch"]},
}


def _normal_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\s]", " ", without_marks).split())


def _publication_years(message: Mapping[str, Any]) -> list[int]:
    """Collect publication years, not Crossref's metadata-record creation date."""
    years: set[int] = set()
    for field in ("published", "issued", "published-print", "published-online"):
        date = message.get(field)
        parts = date.get("date-parts") if isinstance(date, Mapping) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int) and not isinstance(year, bool) and 1400 <= year <= 2200:
                years.add(year)
    return sorted(years)


class DoiVerifyTool:
    """Compare a SourceCard with Crossref; both title and year need evidence."""

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

        # VERIFY: Crossref's single-work endpoint and response fields can change.
        url = f"https://api.crossref.org/works/{quote(source.doi, safe='/')}"
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": "ScholarLoop/0.1 (academic metadata verification)"},
            )
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
            message = {}
        titles = message.get("title")
        metadata_title = (
            next((title for title in titles if isinstance(title, str) and title.strip()), None)
            if isinstance(titles, list)
            else None
        )
        years = _publication_years(message)
        comparisons: list[dict[str, Any]] = []
        mismatch_fields: list[str] = []
        missing_fields: list[str] = []

        returned_doi = message.get("DOI")
        if isinstance(returned_doi, str) and returned_doi.strip():
            doi_matches = normalise_doi(returned_doi) == source.doi
            comparisons.append({"field": "doi", "matches": doi_matches})
            if not doi_matches:
                mismatch_fields.append("doi")

        if metadata_title is not None:
            clean_title = sanitize_untrusted_text(metadata_title, max_chars=500).text
            if clean_title:
                title_matches = _normal_title(source.title) == _normal_title(clean_title)
                comparisons.append({"field": "title", "matches": title_matches})
                if not title_matches:
                    mismatch_fields.append("title")
            else:
                missing_fields.append("title")
        else:
            missing_fields.append("title")
        if years:
            year_matches = source.year in years
            comparisons.append({"field": "year", "matches": year_matches, "metadata_years": years})
            if not year_matches:
                mismatch_fields.append("year")
        else:
            missing_fields.append("year")

        if mismatch_fields:
            status = VerificationStatus.MISMATCH
        elif missing_fields:
            status = VerificationStatus.UNKNOWN
        else:
            status = VerificationStatus.VERIFIED
        return self._result(source, status, comparisons, mismatch_fields, missing_fields)

    def _result(
        self,
        source: SourceCard,
        status: VerificationStatus,
        comparisons: list[dict[str, Any]],
        mismatch_fields: list[str],
        missing_fields: list[str],
    ) -> ToolResult:
        updated = source.model_copy(update={"verification": status})
        payload = {
            "verified": status is VerificationStatus.VERIFIED,
            "mismatch_fields": mismatch_fields,
            "missing_fields": missing_fields,
            "updated_source": updated.model_dump(mode="json"),
            "source_update": {"id": updated.id, "verification": status.value},
            "metadata_compared": bool(comparisons),
            "comparisons": comparisons,
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) > 7_500:
            # Keep a usable verification delta rather than let the registry cut JSON
            # mid-document when an unusually large but valid SourceCard is supplied.
            payload["updated_source"] = None
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return ToolResult(tool=self.name, content=content, truncated=True)
        return ToolResult(tool=self.name, content=content)

    async def aclose(self) -> None:
        """Close the internally created HTTP client only."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
