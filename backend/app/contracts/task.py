"""Task - one unit of work owned by exactly one agent role.

The mandatory-distribution rule is structural: ``role`` is a required enum, so a
task *cannot* be constructed with zero roles or two roles. Plan-level validation
(see :mod:`app.contracts.plan`) then guarantees the dependency graph is sound.
"""

from __future__ import annotations

from pydantic import Field

from app.contracts.common import (
    NonEmptyList,
    NonEmptyStr,
    SectionId,
    Ulid,
    VersionedContract,
    word_count,
)
from app.contracts.enums import AgentRole

__all__ = ["MAX_TASK_INSTRUCTIONS_WORDS", "Task"]

MAX_TASK_INSTRUCTIONS_WORDS = 200


class Task(VersionedContract):
    """A todo item the Planner writes; executed by exactly one agent role."""

    id: Ulid = Field(description="ULID, unique within the plan.")
    title: NonEmptyStr = Field(max_length=120, description="Imperative, one line.")
    role: AgentRole = Field(
        description=(
            "Exactly one of architect|researcher|writer|reviewer|validator. Mandatory "
            "distribution: a task without a role is invalid, and there is no null role."
        )
    )
    section_ids: list[SectionId] = Field(default_factory=list)
    source_ids: list[Ulid] = Field(default_factory=list)
    instructions: NonEmptyStr = Field(max_length=2400)
    acceptance: NonEmptyList[NonEmptyStr] = Field(
        description="Checkable statements; the reviewer/validator grade against these."
    )
    token_budget: int = Field(ge=1, le=2_000_000)
    depends_on: list[Ulid] = Field(
        default_factory=list, description="Task ids that must be 'ok' before this one runs."
    )

    def instruction_word_count(self) -> int:
        """Words in ``instructions``; the Planner is expected to stay concise."""
        return word_count(self.instructions)
