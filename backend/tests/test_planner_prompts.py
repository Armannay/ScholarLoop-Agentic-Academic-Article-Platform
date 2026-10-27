"""``prompts/planner/``: the prompt assets, their budgets and their golden answers.

Prompt material is a deliverable, and every byte of it is paid on every planner call of a
run. These tests keep the assets honest in three ways:

* the two system prompts are *copied* from :mod:`app.planner.prompts`, so the code stays
  the single source of truth and a drift fails the build;
* every asset stays under the step's 700-token ceiling (and the templates far under it);
* the golden answers are validated against the real rubric, the real ``Brief`` and the
  same draft schemas the service sends - a documentation example that is no longer a
  legal answer is a bug, not a stale doc.

The last test pins the ``ContextBuilder`` seam to a Protocol: the planner must be usable
with any builder that implements ``plan``, not only the governor's concrete class.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.contracts.brief import Brief
from app.contracts.enums import ArticleType
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.planner import (
    EVALUATE_INSTRUCTION,
    EVALUATOR_SYSTEM_PROMPT,
    MAX_SYSTEM_PROMPT_TOKENS,
    PLAN_INSTRUCTION,
    PLANNER_SYSTEM_PROMPT,
    REPLAN_INSTRUCTION,
    EvaluationDraft,
    PlanBudget,
    PlanDeltaDraft,
    PlanDraft,
    PlannerContextBuilder,
    PlanValidator,
    build_planner_contexts,
    ensure_prompt_budget,
    evaluation_problems,
)
from app.planner.prompts import prompt_tokens_of
from app.skills.loader import SkillLoader
from tests.planner_support import REPO_ROOT, brief_for, make_binding, ulid_factory

PROMPTS_DIR = REPO_ROOT / "prompts" / "planner"

#: Template ceilings: a template is a layout reminder, not a second system prompt.
TEMPLATE_TOKEN_BUDGET = 200

#: Every file the directory is allowed to contain.
EXPECTED_FILES = frozenset(
    {
        "README.md",
        "system.md",
        "evaluator.md",
        "plan.md",
        "replan.md",
        "evaluate.md",
        "plan.golden.json",
        "replan.golden.json",
        "evaluate.golden.json",
    }
)


def read(name: str) -> str:
    """Text of one asset, read from the repository (never embedded in the test)."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def golden(name: str) -> dict[str, Any]:
    """One golden answer, parsed as the JSON object the model would return."""
    payload = json.loads(read(name))
    assert isinstance(payload, dict)
    return payload


def test_the_directory_is_exactly_the_documented_set() -> None:
    """No stray assets: every file here is loaded, budgeted and tested."""
    assert {path.name for path in PROMPTS_DIR.iterdir() if path.is_file()} == EXPECTED_FILES


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("system.md", PLANNER_SYSTEM_PROMPT),
        ("evaluator.md", EVALUATOR_SYSTEM_PROMPT),
    ],
)
def test_system_prompts_are_verbatim_copies_of_the_code(name: str, expected: str) -> None:
    """The code is authoritative; the asset is a copy that must not drift."""
    assert read(name) == expected


@pytest.mark.parametrize("name", ["system.md", "evaluator.md"])
def test_system_prompts_stay_under_the_seven_hundred_token_ceiling(name: str) -> None:
    """The Step 8 ceiling, checked on the file and not only on the constant."""
    tokens = prompt_tokens_of(read(name))
    assert tokens <= MAX_SYSTEM_PROMPT_TOKENS, f"{name} is {tokens} tokens"
    ensure_prompt_budget(read(name))


@pytest.mark.parametrize(
    ("name", "instruction"),
    [
        ("plan.md", PLAN_INSTRUCTION),
        ("replan.md", REPLAN_INSTRUCTION),
        ("evaluate.md", EVALUATE_INSTRUCTION),
    ],
)
def test_templates_quote_the_instruction_the_service_sends(name: str, instruction: str) -> None:
    """A template that has drifted from the service would teach the wrong layout."""
    text = read(name)
    assert instruction in text
    tokens = prompt_tokens_of(text)
    assert tokens <= TEMPLATE_TOKEN_BUDGET, f"{name} is {tokens} tokens"
    # The planner is never told to produce the agent runtime's answer shape.
    assert "AgentResult" not in text


@pytest.mark.parametrize("name", ["plan.md", "replan.md", "evaluate.md"])
def test_templates_name_the_golden_answer_they_belong_to(name: str) -> None:
    """Each template points at its own golden, so the pair is discoverable."""
    assert name.removesuffix(".md") + ".golden.json" in read(name)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("plan.golden.json", PlanDraft),
        ("replan.golden.json", PlanDeltaDraft),
        ("evaluate.golden.json", EvaluationDraft),
    ],
)
def test_golden_answers_validate_against_the_draft_models(
    name: str, model: type[PlanDraft] | type[PlanDeltaDraft] | type[EvaluationDraft]
) -> None:
    """A golden is a legal wire answer, not prose about one."""
    draft = model.model_validate(golden(name))
    assert draft.model_dump(mode="json")


def test_golden_plan_passes_the_plan_validator(rubric: Rubric, policies: Policies) -> None:
    """The example plan is accepted for a research brief under a normal budget."""
    validator = PlanValidator(rubric, policies)
    brief = brief_for(ArticleType.RESEARCH)
    draft = PlanDraft.model_validate(golden("plan.golden.json"))

    assert (
        validator.problems(draft, brief=brief, budget=PlanBudget(remaining_run_tokens=100_000))
        == []
    )

    plan = validator.build(
        draft,
        brief=brief,
        budget=PlanBudget(remaining_run_tokens=100_000),
        id_factory=ulid_factory(),
    )
    assert len(plan.tasks) <= 8
    assert plan.missing_required_roles() == []
    assert sum(task.token_budget for task in plan.tasks) <= 100_000


def test_golden_delta_applies_to_the_golden_plan(rubric: Rubric, policies: Policies) -> None:
    """The replan example is a delta, and it keeps the merged plan valid."""
    validator = PlanValidator(rubric, policies)
    brief = brief_for(ArticleType.RESEARCH)
    budget = PlanBudget(remaining_run_tokens=100_000)
    plan = validator.build(
        PlanDraft.model_validate(golden("plan.golden.json")),
        brief=brief,
        budget=budget,
        id_factory=ulid_factory(),
    )
    delta = PlanDeltaDraft.model_validate(golden("replan.golden.json"))

    assert validator.delta_problems(plan, delta, brief=brief, budget=budget) == []
    applied = validator.apply_delta(
        plan,
        delta,
        brief=brief,
        budget=budget,
        id_factory=ulid_factory(),
        strategy_change=True,
    )
    assert applied.version == plan.version + 1
    assert applied.strategy_change is True
    assert applied.added and applied.added[0].role.value == "reviewer"


def test_golden_evaluation_passes_the_deterministic_checks(rubric: Rubric) -> None:
    """Every rubric criterion is scored exactly once, and the verdict is coherent."""
    draft = EvaluationDraft.model_validate(golden("evaluate.golden.json"))

    assert evaluation_problems(draft, rubric=rubric) == []
    assert set(draft.scores) == set(rubric.weights())
    # The golden is an honest "not yet": concrete gaps, and therefore no finish.
    assert draft.meets_minimum is False
    assert draft.missing


def test_golden_answers_are_small_enough_to_be_cheap_examples() -> None:
    """Examples travel in documentation and in review; they stay bounded."""
    for name in ("plan.golden.json", "replan.golden.json", "evaluate.golden.json"):
        tokens = prompt_tokens_of(read(name))
        assert tokens <= 1_200, f"{name} is {tokens} tokens"


def test_the_context_builder_seam_is_a_protocol(rubric: Rubric) -> None:
    """Planning depends on ``plan(...)``, not on the governor's concrete class."""

    class MinimalBuilder:
        """A builder that satisfies the seam and nothing else."""

        def plan(self, task: object, run_state: object, **_: object) -> object:  # pragma: no cover
            raise NotImplementedError

    assert isinstance(MinimalBuilder(), PlannerContextBuilder)

    contexts = build_planner_contexts(
        rules_dir=REPO_ROOT / "rules",
        rubric=rubric,
        skills=SkillLoader(REPO_ROOT / "skills"),
        max_input_tokens=make_binding().max_input_tokens,
        max_output_tokens=make_binding().max_output_tokens,
    )
    assert isinstance(contexts.planning, PlannerContextBuilder)
    assert isinstance(contexts.evaluation, PlannerContextBuilder)
    assert contexts.for_kind("evaluate") is contexts.evaluation


def test_a_brief_is_what_the_golden_plan_was_written_against() -> None:
    """Guard the fixture assumption: the goldens target the research rubric."""
    brief: Brief = brief_for(ArticleType.RESEARCH)
    assert brief.must_include == []
    assert brief.article_type is ArticleType.RESEARCH
