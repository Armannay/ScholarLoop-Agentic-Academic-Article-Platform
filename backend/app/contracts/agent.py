"""AgentResult - what every agent returns, always in this shape.

An agent never returns prose to the orchestrator. It returns an ``entry`` (for the
ledger), zero or more ``patches`` (for the article store), ``source_cards`` (for the
source store), rewritten ``sections`` state (reviewer/validator may only set status
and issues) and optionally a ``QualityReport`` (validator only).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.contracts.common import VersionedContract
from app.contracts.enums import AgentRole, ChangeStatus
from app.contracts.ledger import ChangeEntry
from app.contracts.patch import Patch
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard

__all__ = ["REPORT_ONLY_ROLES", "AgentResult"]

#: Roles allowed to return a QualityReport. Only the validator scores the article.
REPORT_ONLY_ROLES: frozenset[AgentRole] = frozenset({AgentRole.VALIDATOR})


class AgentResult(VersionedContract):
    """The complete return value of one agent task execution."""

    entry: ChangeEntry
    patches: list[Patch] = Field(default_factory=list)
    source_cards: list[SourceCard] = Field(default_factory=list)
    sections: list[SectionState] = Field(default_factory=list)
    report: QualityReport | None = None

    @model_validator(mode="after")
    def _internally_consistent(self) -> AgentResult:
        # The ledger also carries the orchestrator's own entries (rollbacks, emergency
        # actions), but an *agent result* is by definition authored by an agent: only
        # the orchestrator may write `role='orchestrator'`, and only for its own work.
        role = self.entry.role
        if not isinstance(role, AgentRole):
            raise ValueError(
                "agents cannot emit orchestrator entries: role 'orchestrator' is "
                "reserved for the orchestrator's own rollback/emergency entries"
            )

        if self.report is not None and role not in REPORT_ONLY_ROLES:
            raise ValueError(
                f"role '{role.value}' may not return a QualityReport; "
                "only the validator scores the article"
            )

        if self.entry.status is ChangeStatus.OK and self.patches and self.entry.patch_ref is None:
            raise ValueError(
                "an entry that carries patches must set patch_ref so the ledger stores a "
                "pointer, not the patch body"
            )

        # The per-patch check below is deliberately the *only* target check: it names
        # the offending section, which is more useful than "targets is empty".
        targets = set(self.entry.targets)
        for patch in self.patches:
            if patch.section_id not in targets:
                raise ValueError(
                    f"patch targets section '{patch.section_id}' which is missing from "
                    "entry.targets"
                )

        source_ids = {card.id for card in self.source_cards}
        if len(source_ids) != len(self.source_cards):
            raise ValueError("duplicate source card ids in one result")

        return self

    @property
    def produced_changes(self) -> bool:
        """True when this result can move the article forward."""
        return self.entry.is_success and bool(self.patches or self.source_cards or self.report)
