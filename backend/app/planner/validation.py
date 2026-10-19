"""Parsing and semantic validation of Planner output - pure, no I/O.

Two boundaries are enforced here, in order:

1. **Shape.** The reply must be one JSON object satisfying the contract
   (:class:`~app.contracts.plan.Plan` / :class:`~app.contracts.verdict.Verdict`)
   after deterministic normalisation. Normalisation never invents *content*: it
   stamps the orchestrator-owned ``version`` and re-mints every task id with the
   injected ULID factory, rewriting ``depends_on`` through the same mapping. A
   model cannot reliably mint ULIDs, so the prompt tells it to use short labels
   (``T1``, ``T2`` ...) and the server owns the ids (ADR-0008). A duplicated label
   is still reported: its ``depends_on`` targets are ambiguous even after reminting.

2. **Semantics.** Things the contract cannot know: required-role coverage, unknown
   section/source ids (a task may only reference cards that exist - HARD RULE 1),
   writers without a section, over-long instructions, and a task-budget sum above
   the caller's cap.

Every failure becomes a short issue string; the Planner feeds the list back in one
bounded repair call instead of failing on the first problem.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.contracts.enums import AgentRole, Recommendation
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.task import MAX_TASK_INSTRUCTIONS_WORDS
from app.contracts.verdict import Verdict
from app.core.ids import is_ulid

__all__ = [
    "MAX_ISSUES",
    "GuardrailOutcome",
    "PlanParseResult",
    "VerdictParseResult",
    "apply_report_guardrails",
    "load_json_object",
    "missing_roles",
    "parse_plan",
    "parse_verdict",
    "validate_plan_semantics",
]

#: The repair prompt lists every issue; capping the list keeps that prompt bounded.
MAX_ISSUES: int = 20

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PlanParseResult:
    """Outcome of parsing one Plan reply. ``plan`` is set only when ``issues`` is empty."""

    plan: Plan | None
    issues: tuple[str, ...]
    payload: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        """True when the reply produced a usable plan."""
        return self.plan is not None and not self.issues


@dataclass(frozen=True, slots=True)
class VerdictParseResult:
    """Outcome of parsing one Verdict reply (guardrails not yet applied)."""

    verdict: Verdict | None
    issues: tuple[str, ...]
    payload: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        """True when the reply produced a usable verdict."""
        return self.verdict is not None and not self.issues


@dataclass(frozen=True, slots=True)
class GuardrailOutcome:
    """A verdict after deterministic report guardrails, plus what changed."""

    verdict: Verdict
    adjustments: tuple[str, ...]


def load_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load exactly one JSON object from a model reply.

    A surrounding ```` ```json ```` fence is tolerated (providers differ on structured
    output hygiene); prose around the JSON is not mined - a reply the schema was
    supposed to shape must stand or fall whole, so the repair prompt sees the truth.
    Returns ``(payload, None)`` or ``(None, issue)``.
    """
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"reply is not valid JSON ({exc.msg} at line {exc.lineno})"
    if not isinstance(payload, dict):
        return None, f"reply must be a JSON object, got {type(payload).__name__}"
    return payload, None


def missing_roles(plan: Plan, require_roles: Collection[AgentRole]) -> tuple[AgentRole, ...]:
    """Required roles owning no task, computed from per-role *counts*.

    Deliberately not ``Plan.missing_required_roles()``: that method subtracts the
    keys of ``role_distribution()``, which contains every role (zero counts
    included), so it can never report a missing role. See CONTRACT-CHANGE-REQUESTS
    in this step's HANDOFF; until the contract is fixed, coverage lives here.
    """
    distribution = plan.role_distribution()
    return tuple(sorted((role for role in require_roles if distribution[role] == 0), key=str))


def validate_plan_semantics(
    plan: Plan,
    *,
    known_section_ids: Collection[str],
    known_source_ids: Collection[str],
    require_roles: Collection[AgentRole],
    budget_cap: int | None,
) -> tuple[str, ...]:
    """Every cross-object defect in ``plan``, as repair-prompt issue strings."""
    issues: list[str] = []

    absent = missing_roles(plan, require_roles)
    if absent:
        named = ", ".join(role.value for role in absent)
        issues.append(f"missing required role(s): {named}; every named role needs >= 1 task")

    known_sections = set(known_section_ids)
    known_sources = set(known_source_ids)
    for task in plan.tasks:
        label = f"task '{task.title}' ({task.role.value})"
        unknown_sections = sorted(set(task.section_ids) - known_sections)
        if unknown_sections:
            issues.append(
                f"{label} targets unknown section(s) {unknown_sections}; "
                f"known sections: {sorted(known_sections)}"
            )
        unknown_sources = sorted(set(task.source_ids) - known_sources)
        if unknown_sources:
            issues.append(
                f"{label} references unknown source id(s) {unknown_sources}; "
                "only existing source cards may be referenced, never invented ones"
            )
        if task.role is AgentRole.WRITER and not task.section_ids:
            issues.append(f"{label} is a writer task with no section_ids; name >= 1 section")
        words = task.instruction_word_count()
        if words > MAX_TASK_INSTRUCTIONS_WORDS:
            issues.append(
                f"{label} instructions are {words} words; keep them "
                f"<= {MAX_TASK_INSTRUCTIONS_WORDS} words"
            )

    if budget_cap is not None:
        total = sum(task.token_budget for task in plan.tasks)
        if total > budget_cap:
            issues.append(
                f"task token budgets sum to {total}, above the cap {budget_cap}; "
                "shrink budgets or drop tasks"
            )
    return _cap_issues(issues)


def parse_plan(
    text: str,
    *,
    expected_version: int,
    id_factory: Callable[[], str],
    known_section_ids: Collection[str],
    known_source_ids: Collection[str],
    require_roles: Collection[AgentRole],
    budget_cap: int | None,
) -> PlanParseResult:
    """Parse, normalise and semantically validate one Plan reply."""
    raw, load_issue = load_json_object(text)
    if raw is None:
        return PlanParseResult(plan=None, issues=(load_issue or "unparsable reply",), payload=None)

    normalised, remint_notes = _normalise_plan_payload(
        raw, expected_version=expected_version, id_factory=id_factory
    )
    try:
        plan = Plan.model_validate(normalised)
    except ValidationError as exc:
        return PlanParseResult(
            plan=None, issues=_cap_issues([*remint_notes, *_validation_issues(exc)]), payload=raw
        )

    issues = _cap_issues(
        [
            *remint_notes,
            *validate_plan_semantics(
                plan,
                known_section_ids=known_section_ids,
                known_source_ids=known_source_ids,
                require_roles=require_roles,
                budget_cap=budget_cap,
            ),
        ]
    )
    if issues:
        return PlanParseResult(plan=None, issues=issues, payload=raw)
    return PlanParseResult(plan=plan, issues=(), payload=raw)


def parse_verdict(text: str) -> VerdictParseResult:
    """Parse one Verdict reply. No normalisation: every field is the model's to own."""
    raw, load_issue = load_json_object(text)
    if raw is None:
        return VerdictParseResult(
            verdict=None, issues=(load_issue or "unparsable reply",), payload=None
        )
    try:
        verdict = Verdict.model_validate(raw)
    except ValidationError as exc:
        return VerdictParseResult(verdict=None, issues=_validation_issues(exc), payload=raw)
    return VerdictParseResult(verdict=verdict, issues=(), payload=raw)


def apply_report_guardrails(
    verdict: Verdict,
    report: QualityReport | None,
    *,
    q_min: int,
) -> GuardrailOutcome:
    """Deterministically override a verdict that contradicts the quality report.

    A Verdict is advice, but advice that says ``finish`` over failing hard gates,
    a sub-``q_min`` Q or open high-severity issues is worse than no advice: the
    loop controller would have to second-guess it. The override is minimal and
    recorded - ``meets_minimum``/``recommendation`` flip, the failing facts are
    appended to ``missing``, the model's own ``risks`` are kept verbatim. A
    verdict that already says ``continue`` is returned untouched.
    """
    if report is None or not verdict.meets_minimum:
        # Nothing to contradict, or the advice already says "not good enough".
        return GuardrailOutcome(verdict=verdict, adjustments=())

    adjustments: list[str] = []
    missing_additions: list[str] = []
    if report.failed_gates:
        adjustments.append("verdict_overridden_failed_gates")
        missing_additions.extend(f"Hard gate failing: {name}" for name in report.failed_gates)
    if q_min > report.Q:
        adjustments.append("verdict_overridden_q_below_minimum")
        missing_additions.append(f"Q is {report.Q}, below the minimum {q_min}")
    high_issues = report.high_severity_issues()
    if high_issues:
        adjustments.append("verdict_overridden_high_severity_issues")
        missing_additions.extend(
            f"High-severity issue open: {issue.id}" for issue in high_issues[:5]
        )
    if not adjustments:
        return GuardrailOutcome(verdict=verdict, adjustments=())

    merged = list(verdict.missing)
    for addition in missing_additions:
        if addition not in merged:
            merged.append(addition)
    corrected = Verdict(
        meets_minimum=False,
        missing=merged[:40],
        risks=list(verdict.risks),
        recommendation=Recommendation.CONTINUE,
    )
    return GuardrailOutcome(verdict=corrected, adjustments=tuple(adjustments))


# -- internals ---------------------------------------------------------------


def _normalise_plan_payload(
    raw: dict[str, Any],
    *,
    expected_version: int,
    id_factory: Callable[[], str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Deep-copy ``raw``, stamp ``version`` and re-mint task ids (see module docstring)."""
    payload: dict[str, Any] = copy.deepcopy(raw)
    payload["version"] = expected_version
    notes: list[str] = []

    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return payload, ()
    mapping: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        new_id = id_factory()
        old = task.get("id")
        if isinstance(old, str) and old:
            if old in mapping:
                notes.append(f"task id '{old}' is used more than once; give every task a unique id")
            else:
                mapping[old] = new_id
        task["id"] = new_id
    unknown_labels: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        depends_on = task.get("depends_on")
        if isinstance(depends_on, list):
            remapped: list[Any] = []
            for item in depends_on:
                if isinstance(item, str) and item in mapping:
                    remapped.append(mapping[item])
                else:
                    # An unmapped non-ULID label would otherwise surface only as a
                    # ULID-format error; name it for what it is (a task that does
                    # not exist in this plan) so the repair call can act on it.
                    if isinstance(item, str) and not is_ulid(item):
                        unknown_labels.add(item)
                    remapped.append(item)
            task["depends_on"] = remapped
    if unknown_labels:
        notes.append(
            "depends_on names unknown task label(s) "
            f"{sorted(unknown_labels)}; dependencies must name tasks in this plan"
        )
    return payload, tuple(notes)


def _validation_issues(exc: ValidationError) -> tuple[str, ...]:
    """One short issue per validation error, located by dotted path."""
    issues: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error.get("loc", ())) or "<document>"
        issues.append(f"{location}: {error.get('msg', 'invalid value')}")
    return _cap_issues(issues)


def _cap_issues(issues: list[str]) -> tuple[str, ...]:
    """Bound the issue list for the repair prompt, saying how many were elided."""
    if len(issues) <= MAX_ISSUES:
        return tuple(issues)
    elided = len(issues) - (MAX_ISSUES - 1)
    return (*issues[: MAX_ISSUES - 1], f"(+{elided} more validation issues of the same kinds)")
