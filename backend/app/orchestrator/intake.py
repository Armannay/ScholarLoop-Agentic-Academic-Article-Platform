"""Intake: deterministic Brief validation and the quick-edit cheap path.

Step 9b, item 1. No LLM ever runs here. A payload is either a usable
:class:`~app.contracts.brief.Brief` (``OK``) or it is missing something only
the user can supply (``NEEDS_INPUT`` with the exact field names). Intake
never guesses a topic, an audience or a word count (HARD RULE 1): a field
that is absent, blank or malformed is reported, not defaulted.

Two request kinds:

* ``brief`` - the normal run. Required fields are checked on the *raw*
  mapping first (so a missing key is named precisely), then the ``Brief``
  contract validates the whole document, then two cheap semantic checks run
  (BCP-47 shape, topic has enough words to be a topic). A rubric, when
  supplied, only *warns* about a target outside the rubric's section ranges:
  the ranges are guidance, the brief's target is the requirement.
* ``quick_edit`` - the cheap path. A single-section edit instruction skips
  the Planner entirely and becomes exactly one :class:`~app.contracts.task.Task`
  owned by one agent role (writer by default). It is still executed through
  the scheduler, applied through the article store and written to the
  ledger, so a quick edit is auditable exactly like planned work.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import Field, ValidationError

from app.contracts.brief import Brief
from app.contracts.common import Contract, NonEmptyStr, SectionId, word_count
from app.contracts.enums import AgentRole
from app.contracts.task import Task
from app.core.ids import new_ulid
from app.core.rubric import Rubric

__all__ = [
    "BRIEF_REQUIRED_FIELDS",
    "IntakeKind",
    "IntakeResult",
    "IntakeStatus",
    "QuickEditRequest",
    "build_quick_edit_task",
    "validate_brief",
    "validate_payload",
    "validate_quick_edit",
]

#: Brief fields with no safe default. Checked against the raw payload first.
BRIEF_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "topic",
    "article_type",
    "audience",
    "language",
    "target_words",
    "citation_style",
)

_MIN_TOPIC_WORDS: Final[int] = 3
_LANGUAGE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_DEFAULT_QUICK_EDIT_BUDGET: Final[int] = 4_000


class IntakeStatus(StrEnum):
    """Outcome of intake validation."""

    OK = "ok"
    NEEDS_INPUT = "needs_input"


class IntakeKind(StrEnum):
    """Which kind of run the payload describes."""

    BRIEF = "brief"
    QUICK_EDIT = "quick_edit"


class QuickEditRequest(Contract):
    """One direct edit to one section, executed without the Planner."""

    section_id: SectionId
    instructions: NonEmptyStr = Field(max_length=2400)
    role: AgentRole = AgentRole.WRITER
    acceptance: list[NonEmptyStr] = Field(
        default_factory=lambda: [
            "Edit applied exactly as instructed",
            "No new unsupported claims, sources or quotes introduced",
        ],
        max_length=8,
    )
    token_budget: int = Field(default=_DEFAULT_QUICK_EDIT_BUDGET, ge=1, le=2_000_000)


class IntakeResult(Contract):
    """The deterministic verdict on one intake payload."""

    status: IntakeStatus
    kind: IntakeKind
    brief: Brief | None = None
    quick_edit: QuickEditRequest | None = None
    missing_fields: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list, description="Stable snake_case codes.")
    warnings: list[str] = Field(default_factory=list, description="Stable snake_case codes.")

    @property
    def ok(self) -> bool:
        """True when a run may start from this payload."""
        return self.status is IntakeStatus.OK


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _validation_fields(exc: ValidationError) -> tuple[list[str], list[str]]:
    """Split a contract failure into (missing field names, problem codes)."""
    missing: list[str] = []
    problems: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "<document>"
        if error.get("type") == "missing":
            missing.append(location)
        else:
            problems.append(f"invalid_{location.replace('.', '_')}")
    return missing, problems


def validate_brief(
    payload: Brief | Mapping[str, Any], *, rubric: Rubric | None = None
) -> IntakeResult:
    """Validate a brief payload deterministically (no LLM, no defaults invented)."""
    if isinstance(payload, Brief):
        raw: dict[str, Any] = payload.model_dump(mode="json", by_alias=True)
        brief: Brief | None = payload
    else:
        raw = dict(payload)
        brief = None

    missing = [name for name in BRIEF_REQUIRED_FIELDS if _is_blank(raw.get(name))]
    if brief is None and not missing:
        try:
            brief = Brief.model_validate(raw)
        except ValidationError as exc:
            extra_missing, problems = _validation_fields(exc)
            return IntakeResult(
                status=IntakeStatus.NEEDS_INPUT,
                kind=IntakeKind.BRIEF,
                missing_fields=sorted(set(missing + extra_missing)),
                problems=sorted(set(problems)),
            )
    if brief is None:
        return IntakeResult(
            status=IntakeStatus.NEEDS_INPUT,
            kind=IntakeKind.BRIEF,
            missing_fields=missing,
            problems=["brief_incomplete"],
        )

    semantic: list[str] = []
    if word_count(brief.topic) < _MIN_TOPIC_WORDS:
        semantic.append("topic_too_vague")
    if not _LANGUAGE_RE.match(brief.language):
        semantic.append("language_not_bcp47")
    if semantic:
        fields = ["topic" if p == "topic_too_vague" else "language" for p in semantic]
        return IntakeResult(
            status=IntakeStatus.NEEDS_INPUT,
            kind=IntakeKind.BRIEF,
            missing_fields=fields,
            problems=semantic,
        )

    warnings: list[str] = []
    if rubric is not None:
        spec = rubric.spec_for(brief.article_type)
        low = spec.abstract.min_words + sum(
            section.min_words for section in spec.sections if section.max_words != 0
        )
        high = spec.abstract.max_words + sum(
            section.max_words or 0 for section in spec.sections if section.max_words != 0
        )
        if not low <= brief.target_words <= high:
            warnings.append("target_words_outside_rubric_range")

    return IntakeResult(
        status=IntakeStatus.OK, kind=IntakeKind.BRIEF, brief=brief, warnings=warnings
    )


def validate_quick_edit(payload: QuickEditRequest | Mapping[str, Any]) -> IntakeResult:
    """Validate a quick-edit payload (the Planner-free cheap path)."""
    if isinstance(payload, QuickEditRequest):
        return IntakeResult(status=IntakeStatus.OK, kind=IntakeKind.QUICK_EDIT, quick_edit=payload)
    raw = dict(payload)
    missing = [name for name in ("section_id", "instructions") if _is_blank(raw.get(name))]
    if missing:
        return IntakeResult(
            status=IntakeStatus.NEEDS_INPUT,
            kind=IntakeKind.QUICK_EDIT,
            missing_fields=missing,
            problems=["quick_edit_incomplete"],
        )
    try:
        request = QuickEditRequest.model_validate(raw)
    except ValidationError as exc:
        extra_missing, problems = _validation_fields(exc)
        return IntakeResult(
            status=IntakeStatus.NEEDS_INPUT,
            kind=IntakeKind.QUICK_EDIT,
            missing_fields=sorted(set(extra_missing)),
            problems=sorted(set(problems)),
        )
    return IntakeResult(status=IntakeStatus.OK, kind=IntakeKind.QUICK_EDIT, quick_edit=request)


def validate_payload(
    payload: Brief | QuickEditRequest | Mapping[str, Any], *, rubric: Rubric | None = None
) -> IntakeResult:
    """Route one payload to brief or quick-edit validation.

    A mapping is a quick edit when it says so (``kind="quick_edit"``) or when
    it carries edit fields (``section_id`` + ``instructions``) and no
    ``topic``. Everything else is treated as a brief, so a malformed brief
    reports its missing brief fields rather than being misread as an edit.
    """
    if isinstance(payload, Brief):
        return validate_brief(payload, rubric=rubric)
    if isinstance(payload, QuickEditRequest):
        return validate_quick_edit(payload)
    kind = payload.get("kind")
    if kind == IntakeKind.QUICK_EDIT.value or (
        "section_id" in payload and "instructions" in payload and "topic" not in payload
    ):
        return validate_quick_edit({k: v for k, v in payload.items() if k != "kind"})
    if kind not in (None, IntakeKind.BRIEF.value):
        return IntakeResult(
            status=IntakeStatus.NEEDS_INPUT,
            kind=IntakeKind.BRIEF,
            missing_fields=["kind"],
            problems=["unknown_request_kind"],
        )
    return validate_brief({k: v for k, v in payload.items() if k != "kind"}, rubric=rubric)


def build_quick_edit_task(
    request: QuickEditRequest, *, id_factory: Callable[[], str] = new_ulid
) -> Task:
    """The single task a quick edit executes. Exactly one role, by contract."""
    return Task(
        id=id_factory(),
        title=f"Quick edit: {request.section_id}"[:120],
        role=request.role,
        section_ids=[request.section_id],
        source_ids=[],
        instructions=request.instructions,
        acceptance=list(request.acceptance),
        token_budget=request.token_budget,
        depends_on=[],
    )
