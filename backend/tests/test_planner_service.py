"""The Planner service over ``MockProvider``: planning, deltas, retries and caps.

Everything asserted here is a contract with the loop controller:

* a good draft becomes a contract-valid plan, once, for every article type;
* a bad draft earns a retry that carries the validator's error list *and nothing else*;
* the retry budget is exactly two, after which the call fails with a reason;
* every prompt is paced by the ``ContextBuilder`` and admitted by the governor;
* the planner never receives the agent runtime's ``AgentResult`` instruction.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.binding import MappingBindingResolver
from app.contracts.enums import AgentRole, ArticleType, BindingRole
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.governor.context_builder import TrimStep
from app.planner import (
    EVALUATOR_SYSTEM_PROMPT,
    MAX_PLANNER_RETRIES,
    MAX_SYSTEM_PROMPT_TOKENS,
    PLAN_INSTRUCTION,
    PLANNER_MAX_OUTPUT_TOKENS,
    PLANNER_SYSTEM_PROMPT,
    PlannerService,
    PlannerStateMissing,
    PlannerUnavailable,
    PlanRejected,
    ensure_prompt_budget,
)
from app.planner.prompts import prompt_tokens_of
from app.providers.mock import Script
from tests.planner_support import (
    RecordingGovernor,
    brief_for,
    make_binding,
    make_contexts,
    make_service,
    plan_payload,
)

SECTIONS = ("introduction", "related-work")


def invalid_plan_payload(*, role: str = "editor") -> dict[str, Any]:
    """A plan draft with one role no contract allows."""
    payload = plan_payload(sections=SECTIONS)
    payload["tasks"][2]["role"] = role
    payload["tasks"][2]["title"] = "editor work for T3"
    return payload


def problems_of(exc: BaseException) -> str:
    """The validator's error list as one searchable string."""
    return " | ".join(getattr(exc, "problems", ())) or str(exc)


def script_repeating(payload: dict[str, Any], times: int = MAX_PLANNER_RETRIES + 1) -> Script:
    """A script that answers the same (bad) payload on every attempt."""
    script = Script()
    for index in range(times):
        script = script.json_reply("planner", index, payload)
    return script


@pytest.mark.parametrize("article_type", list(ArticleType))
async def test_golden_plan_for_each_brief(
    article_type: ArticleType, rubric: Rubric, policies: Policies
) -> None:
    """One clean answer becomes one valid, affordable plan - for every article type."""
    brief = brief_for(article_type)
    sections = rubric.spec_for(article_type).required_ids
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, plan_payload(sections=sections)),
    )

    plan = await harness.service.plan(brief, "# Digest\n- nothing yet", 100_000)

    assert plan.version == 1
    assert [task.role for task in plan.tasks] == [
        AgentRole.ARCHITECT,
        AgentRole.RESEARCHER,
        AgentRole.WRITER,
        AgentRole.VALIDATOR,
    ]
    assert plan.tasks[0].section_ids == sections
    assert sum(task.token_budget for task in plan.tasks) <= 100_000
    assert harness.provider.call_count == 1
    assert harness.service.calls_made == 1
    assert harness.service.usage.total > 0
    assert harness.service.plan_current is plan
    assert harness.service.brief is brief


async def test_request_is_bounded_planner_shaped_and_schema_constrained(
    rubric: Rubric, policies: Policies
) -> None:
    """The request carries the planner prompt, the brief, the digest and hard caps."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )

    await harness.service.plan(brief, "# Digest\n- iteration 0", 100_000)

    request = harness.spy.requests[0]
    prompt = harness.service.last_prompt
    assert prompt is not None
    assert prompt.input_tokens <= prompt.max_input_tokens
    assert prompt.max_input_tokens <= policies.call_input_cap
    assert request.max_output_tokens == PLANNER_MAX_OUTPUT_TOKENS
    assert request.json_schema is not None
    assert request.json_schema["title"] == "PlanDraft"
    prefix = "\n\n".join(block.text for block in request.system_blocks)
    assert "# Root rules" in prefix
    assert "# Planner" in prefix
    assert "# Evaluator" not in prefix
    assert brief.topic in prefix
    message = request.messages[0].text
    assert message.startswith(PLAN_INSTRUCTION)
    assert "## Digest (claude.md)" in message
    assert "- iteration 0" in message
    # The planner is never told to answer with an AgentResult: that instruction belongs
    # to the agent runtime and would make the model produce patches instead of a plan.
    assert "AgentResult" not in message
    assert "never article prose, never patches" in message


async def test_retry_gets_the_error_list_only(rubric: Rubric, policies: Policies) -> None:
    """A retry re-sends the stable prefix and the errors - never the rejected answer."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script()
        .json_reply("planner", 0, invalid_plan_payload())
        .json_reply("planner", 1, plan_payload(sections=SECTIONS)),
    )

    plan = await harness.service.plan(brief, "# Digest\n- iteration 0", 100_000)

    assert plan.version == 1
    assert harness.provider.call_count == 2
    first, retry = harness.spy.requests
    assert retry.system_blocks == first.system_blocks
    retry_text = retry.messages[0].text
    assert retry_text.startswith("The previous answer failed deterministic validation")
    assert "role 'editor' is not one of" in retry_text
    assert "editor work for T3" not in retry_text
    assert "## Digest" not in retry_text
    assert "## Task" not in retry_text
    assert retry.json_schema == first.json_schema
    # Same stable prefix means the same cacheable tokens, measured the same way.
    assert harness.service.last_prompt is not None


async def test_retry_budget_is_two(rubric: Rubric, policies: Policies) -> None:
    """One attempt plus two retries, then the call fails and names the reason."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=script_repeating(invalid_plan_payload()),
    )

    with pytest.raises(PlanRejected) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert caught.value.attempts == MAX_PLANNER_RETRIES + 1
    assert caught.value.tokens.total > 0
    assert harness.provider.call_count == MAX_PLANNER_RETRIES + 1
    assert harness.service.plan_current is None
    assert "role 'editor'" in problems_of(caught.value)


async def test_no_retry_when_retries_are_disabled(rubric: Rubric, policies: Policies) -> None:
    """``max_retries=0`` means exactly one attempt - the loop decides what to do next."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=script_repeating(invalid_plan_payload(), times=1),
        max_retries=0,
    )

    with pytest.raises(PlanRejected) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert caught.value.attempts == 1
    assert harness.provider.call_count == 1


async def test_budget_overflow_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A plan asking for more than the run has left never reaches an agent."""
    brief = brief_for(ArticleType.RESEARCH)
    payload = plan_payload(sections=SECTIONS, token_budget=50_000)
    harness = make_service(
        rubric=rubric, policies=policies, brief=brief, script=script_repeating(payload)
    )

    with pytest.raises(PlanRejected) as caught:
        await harness.service.plan(brief, None, 10_000)

    assert "the remaining run budget is 10000" in problems_of(caught.value)


async def test_too_many_tasks_are_rejected(rubric: Rubric, policies: Policies) -> None:
    """At most eight tasks per iteration, and the excess is reported, not dropped."""
    brief = brief_for(ArticleType.RESEARCH)
    payload = plan_payload(sections=SECTIONS)
    for index in range(5, 10):
        payload["tasks"].append(
            {
                "key": f"T{index}",
                "title": f"Extra writer task {index}",
                "role": "writer",
                "section_ids": [SECTIONS[0]],
                "instructions": "Draft more of the same section.",
                "acceptance": ["The section grew by one paragraph"],
                "token_budget": 500,
                "depends_on": [],
            }
        )
    harness = make_service(
        rubric=rubric, policies=policies, brief=brief, script=script_repeating(payload)
    )

    with pytest.raises(PlanRejected) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert "exceed the per-iteration maximum of 8" in problems_of(caught.value)


async def test_unknown_section_is_rejected(rubric: Rubric, policies: Policies) -> None:
    """A writer cannot be pointed at a section nobody created."""
    brief = brief_for(ArticleType.RESEARCH)
    payload = plan_payload(sections=SECTIONS)
    payload["tasks"][2]["section_ids"] = ["not-in-the-rubric"]
    harness = make_service(
        rubric=rubric, policies=policies, brief=brief, script=script_repeating(payload)
    )

    with pytest.raises(PlanRejected) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert "neither in the rubric for this brief" in problems_of(caught.value)


async def test_schema_errors_are_retried_too(rubric: Rubric, policies: Policies) -> None:
    """An answer that violates the wire schema is a bad answer, not a crash."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script()
        .json_reply("planner", 0, {"goal": "A plan", "tasks": "not-a-list"})
        .json_reply("planner", 1, plan_payload(sections=SECTIONS)),
    )

    plan = await harness.service.plan(brief, None, 100_000)

    assert plan.version == 1
    assert "schema:" in harness.spy.requests[1].messages[0].text


async def test_large_digest_is_trimmed_to_the_binding_cap(
    rubric: Rubric, policies: Policies
) -> None:
    """The ContextBuilder trims digest detail rather than let the prompt overrun."""
    brief = brief_for(ArticleType.RESEARCH)
    binding = make_binding(max_input_tokens=3_000, max_output_tokens=2_000)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=binding,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )
    digest = "# Digest\n" + "\n".join(
        f"- iteration {index}: measured 1200 tokens" for index in range(4_000)
    )

    plan = await harness.service.plan(brief, digest, 100_000)

    prompt = harness.service.last_prompt
    assert prompt is not None
    assert plan.version == 1
    assert prompt.input_tokens <= prompt.max_input_tokens == 3_000
    trimmed = {trim.step for trim in prompt.context_plan.trims}
    assert TrimStep.DIGEST_DETAIL in trimmed


async def test_impossible_input_cap_refuses_before_calling_the_provider(
    rubric: Rubric, policies: Policies
) -> None:
    """A prompt that cannot fit is refused locally: no tokens are spent on it."""
    brief = brief_for(ArticleType.RESEARCH)
    binding = make_binding(max_input_tokens=1_800, max_output_tokens=1_500)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=binding,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )

    with pytest.raises(Exception) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert getattr(caught.value, "code", "") == "context_too_large"
    assert harness.provider.call_count == 0


async def test_output_cap_never_exceeds_the_planner_ceiling(
    rubric: Rubric, policies: Policies
) -> None:
    """Every call sets a completion limit: the smaller of the binding and the ceiling."""
    brief = brief_for(ArticleType.RESEARCH)
    generous = make_binding(max_input_tokens=32_000, max_output_tokens=32_000)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=generous,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )
    await harness.service.plan(brief, None, 100_000)
    assert harness.spy.requests[0].max_output_tokens == PLANNER_MAX_OUTPUT_TOKENS

    tight = make_binding(max_input_tokens=24_000, max_output_tokens=512)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=tight,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )
    await harness.service.plan(brief, None, 100_000)
    assert harness.spy.requests[0].max_output_tokens == 512


async def test_governor_is_consulted_with_the_planner_role(
    rubric: Rubric, policies: Policies
) -> None:
    """The planner is inside the budget system, not beside it."""
    brief = brief_for(ArticleType.RESEARCH)
    governor = RecordingGovernor()
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        governor=governor,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )

    await harness.service.plan(brief, None, 100_000)

    assert len(governor.calls) == 1
    role, task_id = governor.calls[0]
    assert role is BindingRole.PLANNER
    assert task_id.startswith("01")


async def test_binding_is_resolved_at_call_time(rubric: Rubric, policies: Policies) -> None:
    """A binding disabled after construction is never used: no provider call happens."""
    brief = brief_for(ArticleType.RESEARCH)
    binding = make_binding()
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=binding,
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )
    disabled = binding.model_copy(update={"enabled": False})
    harness.service._bindings = MappingBindingResolver({BindingRole.PLANNER: disabled})

    with pytest.raises(PlannerUnavailable) as caught:
        await harness.service.plan(brief, None, 100_000)

    assert caught.value.context["role"] == "planner"
    assert caught.value.code == "planner_unavailable"
    assert harness.provider.call_count == 0


async def test_missing_binding_is_a_user_condition(rubric: Rubric, policies: Policies) -> None:
    """No ``planner`` binding means 'ask the user', not 'retry the provider'."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        binding=make_binding(enabled=False),
        script=Script().json_reply("planner", 0, plan_payload(sections=SECTIONS)),
    )

    with pytest.raises(PlannerUnavailable):
        await harness.service.plan(brief, None, 100_000)

    assert harness.provider.call_count == 0


async def test_replan_returns_a_delta_and_moves_the_plan(
    rubric: Rubric, policies: Policies
) -> None:
    """Replanning costs a delta: the current plan travels, the next version is built."""
    brief = brief_for(ArticleType.RESEARCH)
    delta_payload = {
        "goal": None,
        "added": [
            {
                "key": "N1",
                "title": "Validate the section again",
                "role": "validator",
                "section_ids": [SECTIONS[0]],
                "instructions": "Re-run the gates on the revised introduction.",
                "acceptance": ["Every gate is reported again"],
                "token_budget": 2_000,
                "depends_on": [],
            }
        ],
        "removed": [],
        "reason": "The introduction changed, so validation has to run again.",
    }
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script()
        .json_reply("planner", 0, plan_payload(sections=SECTIONS))
        .json_reply("planner", 1, delta_payload),
    )
    plan = await harness.service.plan(brief, None, 100_000)

    delta = await harness.service.replan("# Digest", None, None, True, plan=plan, budget=None)

    assert delta.base_version == 1
    assert delta.version == 2
    assert delta.strategy_change is True
    assert len(delta.added) == 1
    assert harness.service.plan_current is not None
    assert harness.service.plan_current.version == 2
    replan_text = harness.spy.requests[1].messages[0].text
    assert "## Current plan" in replan_text
    assert "## Strategy change requested" in replan_text
    assert plan.goal in replan_text
    # Deltas are cheaper than replans: the unchanged task ids travel, not their bodies.
    assert plan.tasks[0].id in replan_text


async def test_replan_needs_a_plan(rubric: Rubric, policies: Policies) -> None:
    """Without a plan there is nothing to change."""
    harness = make_service(rubric=rubric, policies=policies, brief=brief_for(ArticleType.REVIEW))

    with pytest.raises(PlannerStateMissing):
        await harness.service.replan(None, None, None, False)


async def test_delta_with_an_unknown_section_is_rejected(
    rubric: Rubric, policies: Policies
) -> None:
    """Deltas obey the same section rules as full plans, and a rejected delta changes nothing."""
    brief = brief_for(ArticleType.RESEARCH)
    bad_delta = {
        "goal": None,
        "added": [
            {
                "key": "N1",
                "title": "Write the invented section",
                "role": "writer",
                "section_ids": ["invented-section"],
                "instructions": "Write it.",
                "acceptance": ["It exists and is cited"],
                "token_budget": 1_000,
                "depends_on": [],
            }
        ],
        "removed": [],
        "reason": "Add the missing section.",
    }
    script = Script().json_reply("planner", 0, plan_payload(sections=SECTIONS))
    for index in range(1, MAX_PLANNER_RETRIES + 2):
        script = script.json_reply("planner", index, bad_delta)
    harness = make_service(rubric=rubric, policies=policies, brief=brief, script=script)
    plan = await harness.service.plan(brief, None, 100_000)

    with pytest.raises(PlanRejected) as caught:
        await harness.service.replan(None, None, None, False, plan=plan)

    assert "neither in the rubric for this brief" in problems_of(caught.value)
    assert harness.service.plan_current is not None
    assert harness.service.plan_current.version == 1


def test_prompt_assets_stay_under_the_700_token_ceiling() -> None:
    """The step's ceiling is enforced by code, not by documentation."""
    assert MAX_SYSTEM_PROMPT_TOKENS == 700
    assert prompt_tokens_of(PLANNER_SYSTEM_PROMPT) <= MAX_SYSTEM_PROMPT_TOKENS
    assert prompt_tokens_of(EVALUATOR_SYSTEM_PROMPT) <= MAX_SYSTEM_PROMPT_TOKENS


def test_prompt_budget_check_rejects_an_oversized_prompt() -> None:
    """A prompt that grows past the ceiling fails at construction time."""
    with pytest.raises(Exception, match="planner prompt asset"):
        ensure_prompt_budget("word " * 4_000)


def test_service_rejects_an_out_of_range_retry_budget(rubric: Rubric, policies: Policies) -> None:
    """The retry cap is part of the loop contract, so it is validated."""
    harness = make_service(rubric=rubric, policies=policies)

    with pytest.raises(ValueError, match="max_retries"):
        PlannerService(
            provider=harness.spy,
            bindings=MappingBindingResolver({BindingRole.PLANNER: make_binding()}),
            governor=RecordingGovernor(),
            contexts=make_contexts(rubric),
            rubric=rubric,
            policies=policies,
            max_retries=MAX_PLANNER_RETRIES + 1,
        )
