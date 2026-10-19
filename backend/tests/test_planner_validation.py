"""Tests for planner output parsing, semantics, guardrails and fallbacks.

Pure layer (no provider): ``app.planner.validation`` + ``app.planner.fallback``.
The Planner never writes article text and never invents ids - these tests pin the
normalisation (server-owned plan version and ULID task ids, ADR-0008), every
semantic defect class that must trigger a repair, and the deterministic report
guardrails that override an over-optimistic verdict.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, Recommendation
from app.contracts.plan import REQUIRED_ROLES, Plan
from app.contracts.quality import QualityReport
from app.contracts.verdict import Verdict
from app.core.ids import UlidFactory, is_ulid
from app.core.rubric import Rubric
from app.planner.errors import PlannerError
from app.planner.fallback import build_fallback_plan, verdict_from_report
from app.planner.validation import (
    apply_report_guardrails,
    load_json_object,
    missing_roles,
    parse_plan,
    parse_verdict,
)
from tests.factories import FIXED_TS_MS, SAMPLE_BRIEF, sample_payloads, sample_ulid

SOURCE_ID = sample_ulid(42)
KNOWN_SECTIONS = {"abstract", "introduction", "methodology", "references"}


def task(
    task_id: str,
    title: str,
    role: str,
    depends_on: list[str],
    *,
    section_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    instructions: str = "Do the work described by the title.",
    token_budget: int = 5_000,
) -> dict[str, Any]:
    """One task as a model would emit it: short label ids, schema_version present."""
    return {
        "schema_version": 1,
        "id": task_id,
        "title": title,
        "role": role,
        "section_ids": section_ids or [],
        "source_ids": source_ids or [],
        "instructions": instructions,
        "acceptance": ["Outcome is checkable"],
        "token_budget": token_budget,
        "depends_on": depends_on,
    }


def full_plan_payload() -> dict[str, Any]:
    """A role-complete plan payload using T-label ids."""
    return {
        "schema_version": 1,
        "version": 7,
        "goal": "A verified article at Q >= 70.",
        "tasks": [
            task("T1", "Outline", "architect", []),
            task("T2", "Find sources", "researcher", ["T1"], section_ids=["introduction"]),
            task(
                "T3",
                "Draft intro",
                "writer",
                ["T2"],
                section_ids=["introduction"],
                source_ids=[SOURCE_ID],
            ),
            task("T4", "Score it", "validator", ["T3"]),
        ],
    }


def parse(payload: dict[str, Any] | str, *, budget_cap: int = 100_000):  # type: ignore[no-untyped-def]
    """``parse_plan`` with the test's fixed known-ids universe."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return parse_plan(
        text,
        expected_version=3,
        id_factory=UlidFactory(timestamp_ms=FIXED_TS_MS, entropy=b"\x00" * 10),
        known_section_ids=KNOWN_SECTIONS,
        known_source_ids={SOURCE_ID},
        require_roles=REQUIRED_ROLES,
        budget_cap=budget_cap,
    )


def make_report(**overrides: Any) -> QualityReport:
    """A quality report from the contract fixtures with selected overrides."""
    payload = dict(sample_payloads()["QualityReport"])
    payload.update(overrides)
    return QualityReport.model_validate(payload)


def make_brief(**overrides: Any) -> Brief:
    """The sample brief with selected overrides."""
    payload = dict(SAMPLE_BRIEF)
    payload.update(overrides)
    return Brief.model_validate(payload)


# -- JSON loading -------------------------------------------------------------


def test_load_json_object_accepts_fence_and_rejects_prose() -> None:
    payload, issue = load_json_object('```json\n{"a": 1}\n```')
    assert (payload, issue) == ({"a": 1}, None)

    payload, issue = load_json_object('Here is the plan: {"a": 1}')
    assert payload is None and issue is not None and "not valid JSON" in issue

    payload, issue = load_json_object("[1, 2]")
    assert payload is None and issue is not None and "JSON object" in issue


# -- Plan parsing ---------------------------------------------------------------


def test_parse_plan_remints_ids_forces_version_and_rewrites_dependencies() -> None:
    result = parse(full_plan_payload())

    assert result.ok, result.issues
    assert result.plan is not None
    assert result.plan.version == 3  # expected_version wins over the model's 7
    assert all(is_ulid(item.id) for item in result.plan.tasks)
    by_role = {item.role: item for item in result.plan.tasks}
    assert by_role[AgentRole.WRITER].depends_on == [by_role[AgentRole.RESEARCHER].id]
    assert by_role[AgentRole.VALIDATOR].depends_on == [by_role[AgentRole.WRITER].id]


def test_parse_plan_duplicate_label_is_an_issue_despite_reminting() -> None:
    payload = full_plan_payload()
    payload["tasks"][1]["id"] = "T1"

    result = parse(payload)

    assert result.plan is None
    assert any("more than once" in issue for issue in result.issues)


def test_parse_plan_cycle_and_dangling_dependency_are_issues() -> None:
    cyclic = full_plan_payload()
    cyclic["tasks"][0]["depends_on"] = ["T4"]
    result = parse(cyclic)
    assert result.plan is None
    assert any("cycle" in issue for issue in result.issues)

    dangling = full_plan_payload()
    dangling["tasks"][3]["depends_on"] = ["T99"]
    result = parse(dangling)
    assert result.plan is None
    assert any("unknown task label" in issue for issue in result.issues)


def test_parse_plan_missing_roles_are_named() -> None:
    """Coverage comes from per-role counts.

    ``Plan.missing_required_roles()`` cannot do this today (it subtracts the keys
    of ``role_distribution()``, which always contains every role) - the planner
    works around it; see CONTRACT-CHANGE-REQUESTS in the step HANDOFF.
    """
    payload = {
        "schema_version": 1,
        "version": 1,
        "goal": "Writer only",
        "tasks": [task("T1", "Draft", "writer", [], section_ids=["introduction"])],
    }
    result = parse(payload)

    assert result.plan is None
    joined = " ".join(result.issues)
    assert "architect" in joined and "researcher" in joined and "validator" in joined

    sample_plan = Plan.model_validate(sample_payloads()["Plan"])  # researcher + writer
    assert missing_roles(sample_plan, REQUIRED_ROLES) == (
        AgentRole.ARCHITECT,
        AgentRole.VALIDATOR,
    )


def test_parse_plan_unknown_section_and_invented_source_are_issues() -> None:
    payload = full_plan_payload()
    payload["tasks"][2]["section_ids"] = ["appendix-z"]
    payload["tasks"][2]["source_ids"] = [sample_ulid(99)]

    result = parse(payload)

    assert result.plan is None
    joined = " ".join(result.issues)
    assert "appendix-z" in joined
    assert "unknown source" in joined


def test_parse_plan_writer_without_section_long_instructions_and_budget_sum() -> None:
    payload = full_plan_payload()
    payload["tasks"][2]["section_ids"] = []
    payload["tasks"][2]["instructions"] = " ".join(["word"] * 201)

    result = parse(payload, budget_cap=9_000)

    assert result.plan is None
    joined = " ".join(result.issues)
    assert "no section_ids" in joined
    assert "201 words" in joined
    assert "above the cap 9000" in joined


# -- Verdict parsing + guardrails -----------------------------------------------


def test_parse_verdict_rejects_contradictory_verdict() -> None:
    result = parse_verdict(
        json.dumps({"meets_minimum": False, "missing": [], "risks": [], "recommendation": "finish"})
    )
    assert result.verdict is None
    assert any("contradicts" in issue for issue in result.issues)


def test_guardrails_override_finish_over_failed_gates() -> None:
    report = make_report(
        Q=90, issues=[], gates={name: name != "no_placeholders" for name in _gate_names()}
    )
    verdict = Verdict(meets_minimum=True, recommendation=Recommendation.FINISH)

    outcome = apply_report_guardrails(verdict, report, q_min=70)

    assert outcome.adjustments == ("verdict_overridden_failed_gates",)
    assert outcome.verdict.meets_minimum is False
    assert outcome.verdict.recommendation is Recommendation.CONTINUE
    assert any("no_placeholders" in item for item in outcome.verdict.missing)


def test_guardrails_override_q_below_minimum_and_high_issues() -> None:
    verdict = Verdict(meets_minimum=True, recommendation=Recommendation.FINISH)

    low_q = apply_report_guardrails(verdict, make_report(Q=50, issues=[]), q_min=70)
    assert "verdict_overridden_q_below_minimum" in low_q.adjustments

    high_issue = apply_report_guardrails(verdict, make_report(Q=90), q_min=70)
    assert "verdict_overridden_high_severity_issues" in high_issue.adjustments


def test_guardrails_leave_consistent_verdicts_untouched() -> None:
    failing = make_report(Q=10)
    continue_verdict = Verdict(
        meets_minimum=False,
        missing=["Everything"],
        recommendation=Recommendation.CONTINUE,
    )
    outcome = apply_report_guardrails(continue_verdict, failing, q_min=70)
    assert outcome.verdict is continue_verdict and outcome.adjustments == ()

    clean = make_report(Q=95, issues=[])
    finish_verdict = Verdict(meets_minimum=True, recommendation=Recommendation.FINISH)
    outcome = apply_report_guardrails(finish_verdict, clean, q_min=70)
    assert outcome.verdict is finish_verdict and outcome.adjustments == ()

    outcome = apply_report_guardrails(finish_verdict, None, q_min=70)
    assert outcome.verdict is finish_verdict and outcome.adjustments == ()


def _gate_names() -> tuple[str, ...]:
    from app.contracts.quality import GATE_NAMES

    return GATE_NAMES


# -- Fallback plan / verdict -----------------------------------------------------


def test_fallback_plan_is_role_complete_budgeted_and_ordered(rubric: Rubric) -> None:
    factory = UlidFactory(timestamp_ms=FIXED_TS_MS, entropy=b"\x00" * 10)
    plan = build_fallback_plan(make_brief(), rubric, id_factory=factory, budget_cap=120_000)

    assert plan.goal.startswith("Fallback template plan")
    assert missing_roles(plan, REQUIRED_ROLES) == ()
    assert sum(item.token_budget for item in plan.tasks) == 120_000
    assert all(item.token_budget >= 1_000 for item in plan.tasks)
    assert [item.role for item in plan.ready_tasks(set())] == [AgentRole.ARCHITECT]
    validator = plan.of_role(AgentRole.VALIDATOR)[0]
    reviewer = plan.of_role(AgentRole.REVIEWER)[0]
    assert validator.depends_on == [reviewer.id]
    # References carries no prose: no writer task drafts it.
    writers = plan.of_role(AgentRole.WRITER)
    assert all(item.section_ids != ["references"] for item in writers)
    # The abstract writer runs after every body writer.
    abstract = next(item for item in writers if item.section_ids == ["abstract"])
    body_writers = {item.id for item in writers if item is not abstract}
    assert set(abstract.depends_on) == body_writers


def test_fallback_plan_deterministic_ids_and_budget_floor(rubric: Rubric) -> None:
    def seeded() -> UlidFactory:
        return UlidFactory(timestamp_ms=FIXED_TS_MS, entropy=b"\x00" * 10)

    first = build_fallback_plan(make_brief(), rubric, id_factory=seeded(), budget_cap=50_000)
    second = build_fallback_plan(make_brief(), rubric, id_factory=seeded(), budget_cap=50_000)
    assert [item.id for item in first.tasks] == [item.id for item in second.tasks]

    with pytest.raises(PlannerError):
        build_fallback_plan(make_brief(), rubric, id_factory=seeded(), budget_cap=1_500)


def test_fallback_researcher_never_invents_without_provided_data(rubric: Rubric) -> None:
    plan = build_fallback_plan(
        make_brief(provided_data=[]),
        rubric,
        id_factory=UlidFactory(timestamp_ms=FIXED_TS_MS, entropy=b"\x00" * 10),
        budget_cap=60_000,
    )
    researcher = plan.of_role(AgentRole.RESEARCHER)[0]
    assert "needs_user_input" in researcher.instructions


def test_verdict_from_report_matches_the_numbers() -> None:
    clean = verdict_from_report(make_report(Q=88, issues=[]), q_min=70)
    assert clean.meets_minimum is True
    assert clean.recommendation is Recommendation.FINISH

    dirty = verdict_from_report(make_report(Q=40), q_min=70)
    assert dirty.meets_minimum is False
    assert dirty.recommendation is Recommendation.CONTINUE
    assert dirty.missing
