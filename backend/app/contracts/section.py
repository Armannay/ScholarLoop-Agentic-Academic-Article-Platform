"""SectionState - the orchestrator's view of one section of the article.

The article itself lives in the article store; this contract is the *state*
(version, hash, word count, citations, status) that the loop controller and the
Planner reason over. Passing this instead of the text is HARD RULE 3.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    Contract,
    IssueId,
    NonEmptyStr,
    SectionId,
    Ulid,
    VersionedContract,
)
from app.contracts.enums import SectionStatus
from app.core.hashing import content_hash

__all__ = ["EMPTY_SECTION_HASH", "CitationRef", "SectionState"]

EMPTY_SECTION_HASH = content_hash("")


class CitationRef(Contract):
    """A citation site in a section.

    ``key`` is the rendered marker (``(Smith, 2020)`` for APA7, ``[1]`` for IEEE).
    ``source_id`` is None only for a marker that has no card yet - exactly the
    condition the both-ways citation gate reports as a high-severity issue.
    """

    key: NonEmptyStr = Field(max_length=120)
    source_id: Ulid | None = Field(default=None, description="None = unresolved citation site.")

    @property
    def is_resolved(self) -> bool:
        """True when the marker maps to a SourceCard."""
        return self.source_id is not None


class SectionState(VersionedContract):
    """Versioned state of one section. ``order`` is the only ordering authority."""

    id: SectionId
    title: NonEmptyStr = Field(max_length=200)
    order: int = Field(ge=0, le=999)
    version: int = Field(default=0, ge=0, description="0 until the first patch is applied.")
    hash: str = Field(
        default=EMPTY_SECTION_HASH,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Hash of the exact text this state describes.",
    )
    words: int = Field(default=0, ge=0, le=200_000)
    citations: list[CitationRef] = Field(default_factory=list, max_length=500)
    status: SectionStatus = SectionStatus.EMPTY
    open_issues: list[IssueId] = Field(default_factory=list, max_length=200)

    @field_validator("hash")
    @classmethod
    def _hash_spelling(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError(f"hash must be spelled 'sha256:<hex>', got {value!r}")
        return value

    @model_validator(mode="after")
    def _status_consistent(self) -> SectionState:
        if self.status is SectionStatus.EMPTY:
            if self.version != 0:
                raise ValueError("an empty section has version 0")
            if self.words != 0:
                raise ValueError("an empty section has 0 words")
            if self.citations:
                raise ValueError("an empty section cannot have citations")
            if self.hash != EMPTY_SECTION_HASH:
                raise ValueError("an empty section must carry the empty-content hash")
        elif self.version == 0:
            raise ValueError("a non-empty section needs version >= 1")
        return self

    @property
    def unresolved_citations(self) -> list[CitationRef]:
        """Citation sites with no SourceCard behind them."""
        return [citation for citation in self.citations if not citation.is_resolved]

    @property
    def is_final(self) -> bool:
        """True once the reviewer/validator have promoted the section to ``final``."""
        return self.status is SectionStatus.FINAL

    def advanced(self, *, version: int, hash_: str, words: int) -> SectionState:
        """Return a copy with new content metrics; status resets to ``draft``.

        Used by the article store when a patch is applied: any content change means
        the previous ``reviewed``/``final`` verdict no longer describes the text.

        The copy is re-validated. ``model_copy(update=...)`` deliberately skips
        validation, which would let ``version=0`` (empty), a malformed hash or a
        negative word count enter the store and break every later invariant check.
        """
        updates = {
            "version": version,
            "hash": hash_,
            "words": words,
            "status": SectionStatus.DRAFT,
        }
        return SectionState.model_validate({**self.model_dump(), **updates})
