"""Deterministic plan validation: the rules the Planner cannot talk its way out of.

Every case here is a *draft* the model could plausibly return, checked against the
contract, the rubric and the budget. The assertions are on the validator's own sentences,
because those sentences are what a retry receives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts.enums import AgentRole, ArticleType
from app.contracts.plan import REQUIRED_ROLES, Plan, PlanConflict
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.planner import (
    MAX_TASKS_PER_PLAN,
    VALID_PLAN_ROLES,
    PlanBudget,
    PlanDeltaDraft,
    PlanDraft,
    PlanValidator,
    plan_draft_schema,
)
from tests.factories import sample_ulids
from tests.planner_support import brief_for, plan_payload


def validator(rubric: Rubric, policies: Policies) -> PlanValidator:
    """A validator over the shipped rubric and policies."""
    return PlanValidator(rubric, policies)


def id_source(count: int = 40, *, offset: int = 0) -> Callable[[], str]:
    """A deterministic, callable source of distinct ULIDs for accepted tasks."""
    return iter(sample_ulids(count + offset)[offset:]).__next__


def task(
    key: str,
    role: str,
    *,
    sections: Iterable[str] = (),
    acceptance: Iterable[str] = ("The section is non-empty and cites a card",),
    budget: int = 1_000,
    depends_on: Iterable[str] = (),
) -> dict[str, Any]:
    """One draft task payload."""
    return {
        "key": key,
        "title": f"{role} work for {key}",
        "role": role,
        "section_ids": list(sections),
        "instructions": "Do the work described by the acceptance criteria.",
        "acceptance": list(acceptance),
        "token_budget": budget,
        "depends_on": list(depends_on),
    }


def draft(*tasks: dict[str, Any], goal: str = "A verified article at Q >= 70.") -> PlanDraft:
    """Parse a set of task payloads into a plan draft."""
    return PlanDraft.model_validate({"goal": goal, "tasks": list(tasks)})


def budget(tokens: int = 100_000, *, max_tasks: int = MAX_TASKS_PER_PLAN) -> PlanBudget:
    """A plan budget wide enough for the good cases."""
    return PlanBudget(remaining_run_tokens=tokens, max_tasks=max_tasks)


@pytest.mark.parametrize("article_type", list(ArticleType))
def test_golden_drafts_build_valid_plans(
    article_type: ArticleType, rubric: Rubric, policies: Policies
) -> None:
    """A well-formed draft for each article type becomes a contract-valid plan."""
    brief = brief_for(article_type)
    sections = rubric.spec_for(article_type).required_ids
    parsed = PlanDraft.model_validate(plan_payload(sections=sections))
    subject = validator(rubric, policies)

    assert subject.problems(parsed, brief=brief, budget=budget()) == []

    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source(), version=1)
    assert plan.version == 1
    assert [item.role for item in plan.tasks] == [
        AgentRole.ARCHITECT,
        AgentRole.RESEARCHER,
        AgentRole.WRITER,
        AgentRole.VALIDATOR,
    ]
    assert all(item.id.startswith("01") for item in plan.tasks)
    assert plan.tasks[1].depends_on == [plan.tasks[0].id]
    assert plan.tasks[0].section_ids == sections
    assert subject.plan_problems(plan, brief=brief, budget=budget()) == []


def test_every_plan_task_has_exactly_one_valid_role(rubric: Rubric, policies: Policies) -> None:
    """An unknown role is named, with the list of roles that would have been accepted."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T2", "researcher", sections=["introduction"]),
        task("T3", "editor", sections=["introduction"]),
        task("T4", "validator", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("role 'editor' is not one of" in problem for problem in problems)
    assert any(VALID_PLAN_ROLES[0] in problem for problem in problems)


def test_planner_may_not_assign_a_task_to_itself(rubric: Rubric, policies: Policies) -> None:
    """``role: planner`` is rejected by name, not treated as an unknown string only."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T2", "planner", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("may not assign a task to itself" in problem for problem in problems)


def test_dependency_cycle_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A cycle is a cycle however plausible the individual edges look."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"], depends_on=["T4"]),
        task("T2", "researcher", sections=["introduction"], depends_on=["T1"]),
        task("T3", "writer", sections=["introduction"], depends_on=["T2"]),
        task("T4", "validator", sections=["introduction"], depends_on=["T3"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("depends_on graph has a cycle" in problem for problem in problems)


def test_missing_acceptance_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A task without acceptance criteria cannot be graded, so it cannot be planned."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"], acceptance=()),
        task("T2", "researcher", sections=["introduction"]),
        task("T3", "writer", sections=["introduction"]),
        task("T4", "validator", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("acceptance is empty" in problem for problem in problems)


def test_a_task_without_a_token_budget_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """Every task states what it may spend; zero is not a budget."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T2", "researcher", sections=["introduction"]),
        task("T3", "writer", sections=["introduction"], budget=0),
        task("T4", "validator", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("token_budget must be a positive integer, got 0" in problem for problem in problems)


def test_eight_tasks_are_allowed(rubric: Rubric, policies: Policies) -> None:
    """The cap is a ceiling, not a target: exactly eight tasks is a valid plan."""
    brief = brief_for(ArticleType.RESEARCH)
    payload = plan_payload(sections=["introduction"])
    for index in range(5, 9):
        payload["tasks"].append(task(f"T{index}", "writer", sections=["introduction"], budget=500))

    problems = validator(rubric, policies).problems(
        PlanDraft.model_validate(payload), brief=brief, budget=budget(200_000)
    )

    assert not any("per-iteration maximum" in problem for problem in problems)


def test_budget_overflow_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """The sum of task budgets must fit what the run has left."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction"]))
    total = sum(item.token_budget for item in parsed.tasks)

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget(total - 1))

    assert any("the remaining run budget is" in problem for problem in problems)
    assert validator(rubric, policies).problems(parsed, brief=brief, budget=budget(total)) == []


def test_too_many_tasks_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """At most eight tasks per iteration - the ninth is named, not silently dropped."""
    brief = brief_for(ArticleType.RESEARCH)
    tasks = [
        task("T1", "architect", sections=["introduction"]),
        task("T2", "researcher", sections=["introduction"]),
        task("T3", "writer", sections=["introduction"]),
        task("T4", "validator", sections=["introduction"]),
    ]
    tasks.extend(task(f"T{index}", "writer", sections=["introduction"]) for index in range(5, 10))
    parsed = draft(*tasks)

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("exceed the per-iteration maximum of 8" in problem for problem in problems)


def test_section_ids_must_exist_or_be_created_by_an_earlier_architect(
    rubric: Rubric, policies: Policies
) -> None:
    """The three cases: unknown, created first, created too late."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)

    unknown = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T2", "writer", sections=["extra-findings"]),
    )
    assert any(
        "neither in the rubric for this brief nor created by an earlier architect task" in problem
        for problem in subject.problems(unknown, brief=brief, budget=budget())
    )

    created_first = draft(
        task("T1", "architect", sections=["introduction", "extra-findings"]),
        task("T2", "writer", sections=["extra-findings"], depends_on=["T1"]),
    )
    assert not any(
        "extra-findings" in problem
        for problem in subject.problems(created_first, brief=brief, budget=budget())
    )

    created_late = draft(
        task("T1", "writer", sections=["extra-findings"]),
        task("T2", "architect", sections=["introduction", "extra-findings"]),
    )
    assert any(
        "neither in the rubric for this brief nor created by an earlier architect task" in problem
        for problem in subject.problems(created_late, brief=brief, budget=budget())
    )


def test_invalid_section_slug_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """Section ids are contract slugs, not prose: the draft cannot even be parsed."""
    with pytest.raises(ValidationError, match="String should match pattern"):
        draft(
            task("T1", "architect", sections=["introduction"]),
            task("T2", "writer", sections=["Introduction"]),
        )


def test_missing_required_role_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A plan without a validator cannot be completed, so it is not runnable."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T2", "researcher", sections=["introduction"]),
        task("T3", "writer", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("no task is assigned to validator" in problem for problem in problems)


def test_duplicate_keys_are_rejected(rubric: Rubric, policies: Policies) -> None:
    """Two tasks with one key make every dependency ambiguous."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(
        task("T1", "architect", sections=["introduction"]),
        task("T1", "writer", sections=["introduction"]),
    )

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("duplicate task keys" in problem for problem in problems)


def test_unknown_dependency_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A dependency on a task that is not in the plan cannot be satisfied."""
    brief = brief_for(ArticleType.RESEARCH)
    parsed = draft(task("T1", "writer", sections=["introduction"], depends_on=["T9"]))

    problems = validator(rubric, policies).problems(parsed, brief=brief, budget=budget())

    assert any("depends_on 'T9' is not a task key" in problem for problem in problems)


def test_plan_delta_applies_to_the_current_plan(rubric: Rubric, policies: Policies) -> None:
    """A delta adds work and drops work without re-sending the plan."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction", "results"]))
    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source())

    added = task("N1", "writer", sections=["results"], depends_on=[plan.tasks[3].id])
    delta_draft = PlanDeltaDraft.model_validate(
        {"added": [added], "removed": [], "reason": "Results section is still missing."}
    )

    assert subject.delta_problems(plan, delta_draft, brief=brief, budget=budget()) == []

    delta = subject.apply_delta(
        plan,
        delta_draft,
        brief=brief,
        budget=budget(),
        id_factory=id_source(80, offset=40),
        strategy_change=True,
    )
    assert delta.base_version == 1
    assert delta.version == 2
    assert delta.strategy_change is True
    assert len(delta.added) == 1

    merged = delta.apply_to(plan)
    assert merged.version == 2
    assert len(merged.tasks) == len(plan.tasks) + 1
    assert subject.plan_problems(merged, brief=brief, budget=budget()) == []


def test_delta_removal_may_name_a_title(rubric: Rubric, policies: Policies) -> None:
    """Titles are accepted for removal because a model is more reliable with them."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction"]))
    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source())

    validator_task = next(task for task in plan.tasks if task.role is AgentRole.VALIDATOR)
    delta_draft = PlanDeltaDraft.model_validate(
        {
            "added": [task("N1", "validator", sections=["introduction"])],
            "removed": [validator_task.title],
            "reason": "Re-run validation with the new gate set.",
        }
    )

    delta = subject.apply_delta(
        plan,
        delta_draft,
        brief=brief,
        budget=budget(),
        id_factory=id_source(80, offset=40),
        strategy_change=False,
    )

    assert len(delta.removed) == 1
    assert delta.removed[0] in {task.id for task in plan.tasks}


def test_delta_cannot_orphan_a_dependency(rubric: Rubric, policies: Policies) -> None:
    """Dropping a task that another task depends on leaves a dangling edge."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction"]))
    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source())
    writer = next(task for task in plan.tasks if task.role is AgentRole.WRITER)

    delta_draft = PlanDeltaDraft.model_validate(
        {"added": [], "removed": [writer.id], "reason": "The writer task is stalled."}
    )

    problems = subject.delta_problems(plan, delta_draft, brief=brief, budget=budget())

    assert any("is not a task key in this plan" in problem for problem in problems)


def test_delta_cannot_leave_a_required_role_unassigned(rubric: Rubric, policies: Policies) -> None:
    """Removing the only researcher leaves an unrunnable plan, so the delta is rejected."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction"]))
    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source())
    researcher = next(task for task in plan.tasks if task.role is AgentRole.RESEARCHER)

    delta_draft = PlanDeltaDraft.model_validate(
        {"added": [], "removed": [researcher.title], "reason": "Research is done."}
    )

    problems = subject.delta_problems(plan, delta_draft, brief=brief, budget=budget())

    assert any("no task is assigned to researcher" in problem for problem in problems)


def test_delta_unknown_removal_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A removal that names nothing in the plan is an error, not a no-op."""
    brief = brief_for(ArticleType.RESEARCH)
    subject = validator(rubric, policies)
    parsed = PlanDraft.model_validate(plan_payload(sections=["introduction"]))
    plan = subject.build(parsed, brief=brief, budget=budget(), id_factory=id_source())

    delta_draft = PlanDeltaDraft.model_validate(
        {"added": [], "removed": ["a task that never existed"], "reason": "Cleanup."}
    )

    problems = subject.delta_problems(plan, delta_draft, brief=brief, budget=budget())

    assert any("neither a task id nor a task title" in problem for problem in problems)


def test_delta_for_another_plan_version_is_rejected() -> None:
    """A stale delta must not silently drop work the plan has since gained."""
    plan = Plan.model_validate(
        {
            "version": 3,
            "goal": "A verified article.",
            "tasks": [
                {
                    "id": sample_ulids(1)[0],
                    "title": "Draft the introduction",
                    "role": "writer",
                    "section_ids": ["introduction"],
                    "instructions": "Draft it.",
                    "acceptance": ["It exists"],
                    "token_budget": 2_000,
                }
            ],
        }
    )
    delta = PlanDeltaDraft.model_validate(
        {"added": [], "removed": [], "reason": "Nothing to change."}
    )
    from app.planner.plan_validator import PlanDelta

    with pytest.raises(PlanConflict):
        PlanDelta(
            base_version=2,
            version=4,
            goal=plan.goal,
            added=[],
            removed=[],
            reason=delta.reason,
        ).apply_to(plan)


def test_schema_marks_every_task_field_required() -> None:
    """The model is told the obligation, not just given a default."""
    schema = plan_draft_schema()
    required = schema["$defs"]["DraftTask"]["required"]
    assert set(required) == {
        "key",
        "title",
        "role",
        "section_ids",
        "instructions",
        "acceptance",
        "token_budget",
        "depends_on",
    }
    assert schema["$defs"]["DraftTask"]["additionalProperties"] is False
    assert schema["properties"]["tasks"]["maxItems"] > MAX_TASKS_PER_PLAN


def test_required_roles_are_the_contract_roles() -> None:
    """The validator's required-role set is the contract's, not a local guess."""
    assert {role.value for role in REQUIRED_ROLES} == {
        "architect",
        "researcher",
        "writer",
        "validator",
    }
