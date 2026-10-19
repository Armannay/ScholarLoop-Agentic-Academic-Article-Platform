"""``lookup_source``: read a SourceCard already in the run's source store.

The tool is the only way an agent can pull a stored card by reference. It returns the
compact card (ids, metadata, <= 60-word snippet) and never article prose, so the
prompt stays bounded (HARD RULE 3) and citations stay traceable to a ULID.

The store is a ``Protocol``: the memory layer's concrete store plugs in without this
module importing it, which keeps the tool testable with a two-line fake (HARD RULE 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol

from app.contracts.source import SourceCard, normalise_doi
from app.core.ids import is_ulid
from app.tools.arguments import reject_unknown_arguments
from app.tools.errors import ToolArgumentError

__all__ = ["LookupSourceTool", "SourceLookup"]


class SourceLookup(Protocol):
    """The two reads a card lookup needs; both return ``None`` when absent."""

    def get(self, source_id: str) -> SourceCard | None: ...

    def find_by_doi(self, doi: str) -> SourceCard | None: ...


def _card_payload(card: SourceCard) -> dict[str, Any]:
    """Fields a writer, reviewer or validator can use - no schema wrapper, no padding."""
    return {
        "id": card.id,
        "title": card.title,
        "authors": list(card.authors),
        "year": card.year,
        "venue": card.venue,
        "doi": card.doi,
        "url": card.url,
        "snippet": card.snippet,
        "verification": card.verification.value,
    }


class LookupSourceTool:
    """``lookup_source``: exactly one of ``source_id`` or ``doi`` selects the card."""

    name = "lookup_source"
    token_cost_hint = 250
    parameters: Final[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "maxLength": 26,
                "description": "ULID of the SourceCard.",
            },
            "doi": {
                "type": "string",
                "maxLength": 255,
                "description": "DOI of the SourceCard (normalised before lookup).",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, lookup: SourceLookup) -> None:
        self._lookup = lookup

    async def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reject_unknown_arguments(arguments, self.parameters["properties"])
        raw_id = arguments.get("source_id")
        raw_doi = arguments.get("doi")
        source_id = raw_id.strip() if isinstance(raw_id, str) else ""
        doi = raw_doi.strip() if isinstance(raw_doi, str) else ""
        if bool(source_id) == bool(doi):
            raise ToolArgumentError("provide exactly one of source_id or doi")

        if source_id:
            if not is_ulid(source_id):
                raise ToolArgumentError("source_id must be a 26-character ULID")
            card = self._lookup.get(source_id)
            selector: dict[str, Any] = {"source_id": source_id}
        else:
            normalised = normalise_doi(doi)
            card = self._lookup.find_by_doi(normalised)
            selector = {"doi": normalised}

        if card is None:
            return {"status": "not_found", **selector}
        return {"status": "ok", "source": _card_payload(card)}
