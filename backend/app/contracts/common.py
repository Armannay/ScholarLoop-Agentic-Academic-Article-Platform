"""Shared contract machinery: base classes, id/timestamp types, word bounds.

Nothing in here knows about a specific domain object. Every contract module
builds on :class:`VersionedContract` (``extra="forbid"``, ``schema_version=1``)
and on the annotated types below, which is what makes the JSON Schemas in
``docs/schemas/`` uniform enough to use as machine-readable documentation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from app.core.clock import to_utc
from app.core.ids import is_ulid

SCHEMA_VERSION: Final = 1

_WORD_RE = re.compile(r"\S+")

#: A section slug, e.g. ``introduction`` or ``related-work``. Section ids are
#: human-stable slugs (not ULIDs) because the rubric, the Planner's todos and the
#: patch anchors all have to agree on them before any row exists in the database.
SECTION_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
#: A ledger target: either a section slug or an entity ULID.
TARGET_REF_PATTERN = r"^(?:[a-z0-9][a-z0-9._-]{0,63}|[0-7][0-9A-HJKMNP-TV-Z]{25})$"

T = TypeVar("T")


def word_count(text: str) -> int:
    """Count whitespace-separated words. The single definition used by every budget."""
    return len(_WORD_RE.findall(text))


def _coerce_utc(value: Any) -> Any:
    """Accept datetime or ISO-8601 string; normalise to aware UTC."""
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, str):
        try:
            return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return value  # let pydantic emit its own, clearer error
    return value


def _validate_ulid(value: str) -> str:
    if not is_ulid(value):
        raise ValueError(f"not a valid 26-character Crockford base32 ULID: {value!r}")
    return value


def word_bounded(limit: int) -> AfterValidator:
    """Validator factory: fail when a string exceeds ``limit`` words.

    Used for ``ChangeEntry.summary`` and ``SourceCard.snippet`` (<= 60 words) -
    the two places where an agent could otherwise smuggle a whole paragraph into
    a digest and silently blow the token budget.
    """

    def _check(value: str) -> str:
        count = word_count(value)
        if count > limit:
            raise ValueError(f"must be at most {limit} words, got {count}")
        return value

    return AfterValidator(_check)


#: Timezone-aware UTC timestamp; naive input is read as UTC, never as local time.
UTCDateTime = Annotated[datetime, BeforeValidator(_coerce_utc)]
#: 26-character ULID string id.
Ulid = Annotated[str, AfterValidator(_validate_ulid)]
#: Non-blank, whitespace-stripped string.
NonEmptyStr = Annotated[str, Field(min_length=1)]
#: Stable slug identifying a section.
SectionId = Annotated[str, Field(min_length=1, max_length=64, pattern=SECTION_ID_PATTERN)]
#: Ledger target: a section slug or an entity ULID.
TargetRef = Annotated[str, Field(min_length=1, max_length=64, pattern=TARGET_REF_PATTERN)]
#: A list that must contain at least one element.
NonEmptyList = Annotated[list[T], Field(min_length=1)]
#: Free text capped at 60 words (ChangeEntry.summary, SourceCard.snippet).
WordBounded60 = Annotated[str, word_bounded(60)]
#: Issue identifier shared by ChangeEntry and QualityReport, e.g. ``ISSUE-citations-003``.
ISSUE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9._:-]{1,39}$"
IssueId = Annotated[str, Field(min_length=2, max_length=40, pattern=ISSUE_ID_PATTERN)]
#: Integer percentage in 0..100.
Score = Annotated[int, Field(ge=0, le=100)]


class Contract(BaseModel):
    """Base for every contract type: unknown fields are a hard error.

    ``extra="forbid"`` is deliberate - a typo in a patch or a plan must fail at
    the boundary instead of quietly changing orchestration behaviour.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        str_min_length=1,
    )


class VersionedContract(Contract):
    """Document-level contract carrying the wire format version."""

    schema_version: Literal[1] = SCHEMA_VERSION
