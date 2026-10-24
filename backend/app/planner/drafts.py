"""The Planner's wire shapes: plan drafts, plan deltas and evaluation drafts.

These models are **not** CONTRACTS v1 documents. They are the intermediate shapes the
Planner returns before the orchestrator owns identity: a model cannot mint ULIDs
reliably, so a draft names tasks with short local ``key`` values (``T1``) and the
validator maps them to ULIDs. The same split exists in the agent runtime, where the
runtime fills ``entry.id``, ``entry.iter`` and ``entry.tokens``.

Each model doubles as the JSON schema the provider call is constrained to - the schema
is generated from the model, so a field cannot drift out of the prompt.

``extra="forbid"`` comes from :class:`~app.contracts.common.Contract`, so an invented
key is a validation error rather than a silently ignored field.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Final

from pydantic import Field

from app.contracts.common import Contract, NonEmptyStr, Score, SectionId
from app.contracts.enums import Recommendation

__all__ = [
    "DRAFT_KEY_PATTERN",
    "DraftTask",
    "EvaluationDraft",
    "PlanDeltaDraft",
    "PlanDraft",
    "evaluation_schema",
    "plan_delta_schema",
    "plan_draft_schema",
]

#: Local task key inside one draft: ``T1``, ``task-3``. Mapped to a ULID on acceptance.
DRAFT_KEY_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9_-]{0,15}$"
#: A draft may be over the per-iteration cap so the validator can *say so*; the schema
#: ceiling exists only to bound the answer, not to replace the error message.
MAX_DRAFT_TASKS: Final[int] = 16
MAX_ACCEPTANCE_ITEMS: Final[int] = 20
MAX_TASK_INSTRUCTIONS_CHARS: Final[int] = 2400


class DraftTask(Contract):
    """One task as the Planner writes it, before ids and the run state are attached.

    ``role`` is a plain string here on purpose: an unknown role must produce the
    readable validator error ("role 'planner' is not ...") instead of an enum
    validation failure that names only a hidden value.
    """

    key: Annotated[str, Field(min_length=1, max_length=16, pattern=DRAFT_KEY_PATTERN)] = Field(
        description="Short local id, e.g. T1. Referenced by other tasks' depends_on."
    )
    title: NonEmptyStr = Field(max_length=120, description="Imperative, one line.")
    role: NonEmptyStr = Field(
        max_length=32,
        description="Exactly one of architect|researcher|writer|reviewer|validator.",
    )
    section_ids: list[SectionId] = Field(
        default_factory=list,
        max_length=64,
        description="Sections this task reads or edits.",
    )
    instructions: NonEmptyStr = Field(
        max_length=MAX_TASK_INSTRUCTIONS_CHARS,
        description="What to do, concise. No article prose.",
    )
    acceptance: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=MAX_ACCEPTANCE_ITEMS,
        description="Checkable statements the reviewer/validator grade against.",
    )
    token_budget: int = Field(
        default=0,
        ge=0,
        le=2_000_000,
        description="Tokens this task may spend. Must be positive.",
    )
    depends_on: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=16,
        description="Keys of tasks that must finish first.",
    )


class PlanDraft(Contract):
    """A whole plan, before validation."""

    goal: NonEmptyStr = Field(max_length=800, description="What 'done' means for this run.")
    tasks: list[DraftTask] = Field(
        default_factory=list,
        max_length=MAX_DRAFT_TASKS,
        description="At most eight tasks per iteration.",
    )


class PlanDeltaDraft(Contract):
    """A change to an existing plan: added tasks and removed task ids.

    A delta is what replanning returns because re-sending unchanged tasks costs tokens
    and risks silently rewriting work that is already running.
    """

    goal: str | None = Field(
        default=None, max_length=800, description="New goal, or null to keep the current one."
    )
    added: list[DraftTask] = Field(
        default_factory=list,
        max_length=MAX_DRAFT_TASKS,
        description="New tasks only. A task that already exists is not 'added'.",
    )
    removed: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=32,
        description="Ids (or keys) of existing tasks to drop.",
    )
    reason: NonEmptyStr = Field(
        max_length=400,
        description="Why the plan changes, in one sentence.",
    )


class EvaluationDraft(Contract):
    """The Planner's judgement, before the deterministic gates are merged in."""

    scores: dict[str, Score] = Field(
        default_factory=dict,
        description="Every rubric criterion -> 0..100. No missing, no extra key.",
    )
    missing: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=40,
        description="Concrete, checkable requirements that are absent or failing.",
    )
    risks: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=40,
        description="What would make a reader distrust the article if it finished now.",
    )
    meets_minimum: bool = Field(
        description="The Planner's own bar. The hard gates are the real one."
    )
    recommendation: Recommendation = Field(description="finish or continue.")


def _require_all(item: dict[str, Any], names: list[str]) -> None:
    """Mark every named property required, so the schema states the obligation.

    The draft models give optional fields defaults (a missing acceptance list must
    produce a *validator* message, not a pydantic error the retry cannot act on), but
    the model should still be told that the fields are mandatory.
    """
    item["required"] = list(names)


def plan_draft_schema() -> dict[str, Any]:
    """JSON schema for one plan draft, with every task field marked required."""
    schema: dict[str, Any] = PlanDraft.model_json_schema(mode="validation")
    schema["title"] = "PlanDraft"
    task = schema.get("$defs", {}).get("DraftTask")
    if isinstance(task, dict):
        _require_all(
            task,
            [
                "key",
                "title",
                "role",
                "section_ids",
                "instructions",
                "acceptance",
                "token_budget",
                "depends_on",
            ],
        )
    return copy.deepcopy(schema)


def plan_delta_schema() -> dict[str, Any]:
    """JSON schema for one plan delta."""
    schema: dict[str, Any] = PlanDeltaDraft.model_json_schema(mode="validation")
    schema["title"] = "PlanDeltaDraft"
    task = schema.get("$defs", {}).get("DraftTask")
    if isinstance(task, dict):
        _require_all(
            task,
            [
                "key",
                "title",
                "role",
                "section_ids",
                "instructions",
                "acceptance",
                "token_budget",
                "depends_on",
            ],
        )
    return copy.deepcopy(schema)


def evaluation_schema() -> dict[str, Any]:
    """JSON schema for one evaluation draft."""
    schema: dict[str, Any] = EvaluationDraft.model_json_schema(mode="validation")
    schema["title"] = "EvaluationDraft"
    return copy.deepcopy(schema)
