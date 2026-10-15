"""SourceCard - the atomic, auditable unit of a citation.

HARD RULE 1 lives here. ``snippet`` is capped at 60 words so a card can travel in
a digest without blowing the budget, and ``verification`` starts at ``unknown``:
only the validator may move it to ``verified``, and only with evidence.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    NonEmptyStr,
    Ulid,
    VersionedContract,
    WordBounded60,
    word_count,
)
from app.contracts.enums import VerificationStatus

__all__ = ["SNIPPET_WORD_LIMIT", "SourceCard", "normalise_doi"]

SNIPPET_WORD_LIMIT = 60

_DOI_PREFIX_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_DOI_SHAPE_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def normalise_doi(value: str) -> str:
    """Strip resolver prefixes and lower-case a DOI so two spellings compare equal."""
    return _DOI_PREFIX_RE.sub("", value.strip()).lower()


class SourceCard(VersionedContract):
    """One reference, with enough metadata to verify it and too little room to pad.

    ``snippet`` is the only free-text field. ``url`` must be an absolute http(s)
    URL, and ``doi`` is normalised on validation so deduplication is reliable.
    """

    id: Ulid = Field(description="ULID of this card; referenced by tasks' source_ids.")
    title: NonEmptyStr = Field(max_length=500)
    authors: list[NonEmptyStr] = Field(
        default_factory=list, description="Family-name-first, as printed; [] only if unattributed."
    )
    year: int = Field(ge=1400, le=2200)
    venue: NonEmptyStr = Field(max_length=300, description="Journal, conference or publisher.")
    doi: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=2000)
    snippet: WordBounded60 = Field(
        description=f"Quoted or paraphrased support, <= {SNIPPET_WORD_LIMIT} words."
    )
    verification: VerificationStatus = VerificationStatus.UNKNOWN

    @field_validator("doi")
    @classmethod
    def _normalise_doi(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = normalise_doi(value)
        if not _DOI_SHAPE_RE.match(cleaned):
            raise ValueError(f"malformed DOI (expected 10.xxxx/suffix): {value!r}")
        return cleaned

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError(f"url must be an absolute http(s) URL: {value!r}")
        return value

    @model_validator(mode="after")
    def _require_locator(self) -> SourceCard:
        if self.doi is None and self.url is None:
            raise ValueError(
                "a SourceCard needs a DOI or a URL: an unlocatable reference cannot be verified"
            )
        return self

    @property
    def snippet_words(self) -> int:
        """Word count of ``snippet``, for digest budgeting."""
        return word_count(self.snippet)

    @property
    def is_locatable(self) -> bool:
        """True when a human (or a live verifier) could reach the source."""
        return self.doi is not None or self.url is not None
