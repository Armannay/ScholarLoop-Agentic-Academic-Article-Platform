"""Brief - the user's intake payload. The loop's only source of requirements."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from app.contracts.common import NonEmptyStr, VersionedContract, word_count
from app.contracts.enums import ArticleType, CitationStyle

__all__ = ["MAX_TARGET_WORDS", "MIN_TARGET_WORDS", "Brief"]

MIN_TARGET_WORDS = 300
MAX_TARGET_WORDS = 30_000


class Brief(VersionedContract):
    """What the user asked for, before any agent has run.

    ``provided_data`` holds user-supplied data or quotes verbatim: agents may cite
    it, and the validator treats those citations as ``verified`` by construction.
    An empty ``provided_data`` means the researcher must find real sources or
    return ``needs_user_input`` - it never means "invent one".
    """

    topic: NonEmptyStr = Field(description="One-sentence statement of the article's subject.")
    article_type: ArticleType = Field(description="Selects the rubric's required sections.")
    audience: NonEmptyStr = Field(description="Who reads this, e.g. 'CS undergraduates'.")
    language: NonEmptyStr = Field(
        min_length=2, max_length=35, description="BCP-47 tag, e.g. 'en' or 'en-GB'."
    )
    target_words: int = Field(
        ge=MIN_TARGET_WORDS,
        le=MAX_TARGET_WORDS,
        description="Target total length; the total-words gate allows +-15 percent.",
    )
    citation_style: CitationStyle
    must_include: list[NonEmptyStr] = Field(default_factory=list)
    must_avoid: list[NonEmptyStr] = Field(default_factory=list)
    provided_data: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Verbatim user data/quotes. Empty is allowed; fabricated sources are not.",
    )

    @model_validator(mode="after")
    def _no_contradictory_constraints(self) -> Brief:
        overlap = {item.casefold() for item in self.must_include} & {
            item.casefold() for item in self.must_avoid
        }
        if overlap:
            raise ValueError(
                f"must_include and must_avoid overlap, so the brief is unsatisfiable: "
                f"{sorted(overlap)}"
            )
        for item in (*self.must_include, *self.must_avoid):
            if word_count(item) > 40:
                raise ValueError(
                    "must_include/must_avoid entries are instructions, keep each <= 40 words"
                )
        return self

    @property
    def word_tolerance_low(self) -> int:
        """Lower bound of the total-words gate (target - 15 percent)."""
        return int(self.target_words * 0.85)

    @property
    def word_tolerance_high(self) -> int:
        """Upper bound of the total-words gate (target + 15 percent)."""
        return int(self.target_words * 1.15)


AnnotatedBrief = Annotated[Brief, Field(description="CONTRACTS v1 Brief")]
