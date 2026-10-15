"""ChangeEntry and TokenUsage - the ledger's only write shape.

The ledger is append-only: entries are never mutated, only appended and read back
into a bounded digest. ``summary`` is capped at 60 words for exactly that reason.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from app.contracts.common import (
    Contract,
    IssueId,
    TargetRef,
    Ulid,
    VersionedContract,
    WordBounded60,
)
from app.contracts.enums import AgentRole, ChangeStatus

__all__ = ["ChangeEntry", "TokenUsage", "patch_ref_for"]

PATCH_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,199}$"


def patch_ref_for(patch_id: str, *, patch_version: int | None = None) -> str:
    """Build the canonical ``patch_ref`` string: a pointer, never the patch body."""
    return f"patch:{patch_id}" if patch_version is None else f"patch:{patch_id}@v{patch_version}"


class TokenUsage(Contract):
    """Token accounting for one provider call.

    Named ``in_`` in Python because ``in`` is a keyword, but the wire format is
    exactly ``{"in": n, "out": n, "cached": n}`` (``serialize_by_alias``), matching
    CONTRACTS v1 and the provider usage blocks.

    Non-overlapping by definition: ``in_`` counts prompt tokens billed at full rate,
    ``cached`` counts prompt tokens served from the cache (billed cheaper), ``out``
    counts completion tokens. ``total`` is therefore the true token volume and is
    what the budget caps measure; nothing is counted twice.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        validate_assignment=True,
    )

    in_: int = Field(alias="in", ge=0, description="Prompt tokens billed at full rate.")
    out: int = Field(ge=0, description="Completion tokens.")
    cached: int = Field(default=0, ge=0, description="Prompt tokens served from cache.")

    @property
    def total(self) -> int:
        """All tokens consumed: full-rate prompt + cached prompt + completion."""
        return self.in_ + self.cached + self.out

    @property
    def full_rate(self) -> int:
        """Tokens billed at full rate (prompt + completion)."""
        return self.in_ + self.out

    @property
    def cache_hit_ratio(self) -> float:
        """Share of prompt tokens served from cache, 0.0 when there was no prompt."""
        prompt = self.in_ + self.cached
        return 0.0 if prompt == 0 else self.cached / prompt


class ChangeEntry(VersionedContract):
    """One agent action, as recorded in the append-only ledger."""

    id: Ulid
    iter: int = Field(ge=0, le=10_000, description="Loop iteration that produced this entry.")
    task_id: Ulid = Field(description="Task this work belongs to.")
    role: AgentRole = Field(description="The single role that produced the entry.")
    targets: list[TargetRef] = Field(
        default_factory=list, max_length=64, description="Sections/entities touched."
    )
    summary: WordBounded60 = Field(description="<= 60 words; the only text the digest keeps.")
    patch_ref: str | None = Field(default=None, pattern=PATCH_REF_PATTERN)
    tokens: TokenUsage
    issues_opened: list[IssueId] = Field(default_factory=list, max_length=64)
    issues_closed: list[IssueId] = Field(default_factory=list, max_length=64)
    status: ChangeStatus = ChangeStatus.OK

    @property
    def is_success(self) -> bool:
        """True when the entry represents progress (only ``ok`` counts)."""
        return self.status is ChangeStatus.OK
