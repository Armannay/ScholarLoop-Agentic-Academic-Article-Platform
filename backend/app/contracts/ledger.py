"""ChangeEntry and TokenUsage - the ledger's only write shape.

The ledger is append-only: entries are never mutated, only appended and read back
into a bounded digest. ``summary`` is capped at 60 words for exactly that reason.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.contracts.common import (
    Contract,
    IssueId,
    TargetRef,
    Ulid,
    VersionedContract,
    WordBounded60,
)
from app.contracts.enums import ORCHESTRATOR_ROLE, AgentRole, ChangeStatus

__all__ = ["ChangeEntry", "EntryRole", "TokenUsage", "patch_ref_for", "role_name"]

PATCH_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,199}$"

#: ``ChangeEntry.role``: one of the five agent roles, or the orchestrator's own role.
#: The literal is spelled out rather than added to :class:`AgentRole` on purpose - a
#: task owner must stay an agent, so widening the enum would let a ``Plan`` assign work
#: to a non-agent.
EntryRole = AgentRole | Literal["orchestrator"]


def role_name(role: EntryRole) -> str:
    """Wire name of a ledger role: the enum's value, or ``'orchestrator'`` itself."""
    return role.value if isinstance(role, AgentRole) else role


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
    """One action, as recorded in the append-only ledger.

    ``role`` names who produced the entry: one of the five agent roles, or
    ``'orchestrator'`` for the actions the loop takes itself (rolling back to the best
    snapshot, an emergency stop or repair). Agents may never emit the latter - see
    :class:`~app.contracts.agent.AgentResult`.
    """

    id: Ulid
    iter: int = Field(ge=0, le=10_000, description="Loop iteration that produced this entry.")
    task_id: Ulid = Field(description="Task this work belongs to.")
    role: EntryRole = Field(
        description=(
            "Who produced the entry: one of the five agent roles, or 'orchestrator' "
            "for a rollback or emergency action taken by the loop itself."
        )
    )
    targets: list[TargetRef] = Field(
        default_factory=list, max_length=64, description="Sections/entities touched."
    )
    summary: WordBounded60 = Field(description="<= 60 words; the only text the digest keeps.")
    patch_ref: str | None = Field(default=None, pattern=PATCH_REF_PATTERN)
    tokens: TokenUsage
    issues_opened: list[IssueId] = Field(default_factory=list, max_length=64)
    issues_closed: list[IssueId] = Field(default_factory=list, max_length=64)
    status: ChangeStatus = ChangeStatus.OK

    @field_validator("role", mode="before")
    @classmethod
    def _known_role(cls, value: Any) -> Any:
        """Accept the five agent roles or ``'orchestrator'``; name the set on failure.

        The union alone would already reject an unknown name, but the Planner (which
        plans and evaluates and never writes the ledger) is the interesting mistake, so
        the error lists every role an entry may carry.
        """
        if isinstance(value, AgentRole):
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if candidate == ORCHESTRATOR_ROLE:
                return ORCHESTRATOR_ROLE
            try:
                return AgentRole(candidate)
            except ValueError:
                allowed = ", ".join([role.value for role in AgentRole] + [ORCHESTRATOR_ROLE])
                raise ValueError(f"role must be one of: {allowed} (got {value!r})") from None
        return value

    @model_validator(mode="after")
    def _orchestrator_never_authors_text(self) -> ChangeEntry:
        """Orchestrator entries record state changes, never article text.

        ``patch_ref`` points at a :class:`~app.contracts.patch.Patch` an *agent*
        authored (HARD RULE 3). A rollback or an emergency action mutates the article
        store directly, so an orchestrator entry carrying a ``patch_ref`` would
        misattribute authored text to the loop.
        """
        if self.role == ORCHESTRATOR_ROLE and self.patch_ref is not None:
            raise ValueError(
                "orchestrator entries must not carry patch_ref: rollbacks and emergency "
                "actions change state, they never author article text"
            )
        return self

    @property
    def role_name(self) -> str:
        """Wire name of ``role``: the agent role's value, or ``'orchestrator'``."""
        return role_name(self.role)

    @property
    def is_orchestrator(self) -> bool:
        """True when the orchestrator itself recorded this entry."""
        return self.role == ORCHESTRATOR_ROLE

    @property
    def agent_role(self) -> AgentRole | None:
        """``role`` as an :class:`AgentRole`, or ``None`` for orchestrator entries."""
        return self.role if isinstance(self.role, AgentRole) else None

    @property
    def is_success(self) -> bool:
        """True when the entry represents progress (only ``ok`` counts)."""
        return self.status is ChangeStatus.OK
