"""Evaluation: the model judges, the gates decide, and the merge can only say no.

Each test fixes one input of the merge and asserts the resulting ``Verdict``, because the
loop controller acts on the verdict and on nothing else. The recurring theme is that no
model answer - however confident - can pass a hard gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.contracts.enums import ArticleType, LoopAction, Recommendation
from app.contracts.loop import LoopDecision
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.planner import (
    EVALUATOR_MAX_OUTPUT_TOKENS,
    MAX_PLANNER_RETRIES,
    Evaluation,
    EvaluationRejected,
    PlannerStateMissing,
    evaluation_problems,
    merge_verdict,
    weighted_q,
)
from app.planner.drafts import EvaluationDraft
from app.planner.probes import build_probe_report
from app.providers.mock import Script
from tests.planner_support import (
    ARTICLE_LAYOUT,
    article_view,
    brief_for,
    full_view,
    make_service,
    quality_report,
)

#: The rubric's criteria, in rubric order, each scored 80 by default.
HIGH_SCORES: dict[str, int] = {
    "contribution_and_gap": 80,
    "coherence": 80,
    "claims_supported": 80,
    "methods_clarity": 80,
    "results_match_claims": 80,
    "literature_coverage": 80,
    "language": 80,
    "consistency": 80,
    "limitations_and_ethics": 80,
}


def evaluation_payload(**overrides: Any) -> dict[str, Any]:
    """A well-formed evaluation draft: all criteria scored, done, nothing missing."""
    payload: dict[str, Any] = {
        "scores": dict(HIGH_SCORES),
        "missing": [],
        "risks": ["A reader may want a larger sample than the case study reports."],
        "meets_minimum": True,
        "recommendation": Recommendation.FINISH.value,
    }
    payload.update(overrides)
    return payload


def failing_view(rubric: Rubric) -> Any:
    """A view whose ``required_sections_present`` gate fails: no results section.

    The results words move into the discussion so the word-count gate stays green and the
    test isolates one failure. (``rubric`` is unused but keeps the call sites symmetric.)
    """
    del rubric
    layout = [
        ("discussion", 1_300) if section_id == "discussion" else (section_id, words)
        for section_id, words in ARTICLE_LAYOUT
        if section_id != "results"
    ]
    return article_view(brief_for(ArticleType.RESEARCH), layout=layout)


async def test_every_gate_green_allows_finish(rubric: Rubric, policies: Policies) -> None:
    """A complete article with good scores is allowed to finish, with nothing missing."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, evaluation_payload()),
    )
    probes = build_probe_report(full_view(brief), rubric)

    evaluation = await harness.service.evaluate_detailed(None, None, probes)

    assert isinstance(evaluation, Evaluation)
    assert evaluation.verdict.meets_minimum is True
    assert evaluation.verdict.recommendation is Recommendation.FINISH
    assert evaluation.verdict.missing == []
    assert evaluation.failed_gates == ()
    assert evaluation.q == 80
    assert evaluation.attempts == 1
    assert harness.provider.call_count == 1


async def test_a_failing_gate_overrules_a_confident_finish(
    rubric: Rubric, policies: Policies
) -> None:
    """The model says 'finish'; the probe says a required section is missing."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, evaluation_payload()),
    )
    probes = build_probe_report(failing_view(rubric), rubric)

    evaluation = await harness.service.evaluate_detailed(None, None, probes)

    assert evaluation.failed_gates == ("required_sections_present",)
    assert evaluation.verdict.meets_minimum is False
    assert evaluation.verdict.recommendation is Recommendation.CONTINUE
    assert any(
        "hard gate 'required_sections_present' fails" in line for line in evaluation.verdict.missing
    )
    assert any("results" in line for line in evaluation.verdict.missing)
    assert evaluation.planner_meets_minimum is True


async def test_q_below_the_policy_minimum_blocks_completion(
    rubric: Rubric, policies: Policies
) -> None:
    """Low scores are named with the arithmetic, so the next plan knows the gap."""
    brief = brief_for(ArticleType.RESEARCH)
    weak = dict.fromkeys(HIGH_SCORES, 40)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, evaluation_payload(scores=weak)),
    )

    evaluation = await harness.service.evaluate_detailed(
        None, None, build_probe_report(full_view(brief), rubric)
    )

    assert evaluation.q == 40
    assert evaluation.verdict.meets_minimum is False
    assert any(
        f"below the q_min of {policies.q_min}" in line for line in evaluation.verdict.missing
    )


async def test_open_high_severity_issues_block_completion(
    rubric: Rubric, policies: Policies
) -> None:
    """A validator issue nobody fixed is a hard stop even with every gate green."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, evaluation_payload()),
    )

    evaluation = await harness.service.evaluate_detailed(
        None,
        quality_report(issues=2),
        build_probe_report(full_view(brief), rubric),
    )

    assert evaluation.verdict.meets_minimum is False
    assert any("high-severity issue" in line for line in evaluation.verdict.missing)


async def test_model_missing_items_survive_the_merge(rubric: Rubric, policies: Policies) -> None:
    """The model's own concrete requirements are kept, deduped and in order."""
    brief = brief_for(ArticleType.RESEARCH)
    payload = evaluation_payload(
        meets_minimum=False,
        recommendation=Recommendation.CONTINUE.value,
        missing=["The limitations section names no threat to validity."],
    )
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, payload),
    )

    evaluation = await harness.service.evaluate_detailed(
        None, None, build_probe_report(full_view(brief), rubric)
    )

    assert evaluation.verdict.meets_minimum is False
    assert "The limitations section names no threat to validity." in evaluation.verdict.missing
    assert evaluation.verdict.risks == [
        "A reader may want a larger sample than the case study reports."
    ]


async def test_finish_without_minimum_is_rejected_and_retried(
    rubric: Rubric, policies: Policies
) -> None:
    """A contradictory answer earns a retry carrying the deterministic error list."""
    brief = brief_for(ArticleType.RESEARCH)
    contradictory = evaluation_payload(
        meets_minimum=False, recommendation=Recommendation.FINISH.value
    )
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script()
        .json_reply("planner", 0, contradictory)
        .json_reply(
            "planner",
            1,
            evaluation_payload(
                meets_minimum=False,
                recommendation=Recommendation.CONTINUE.value,
                missing=["The discussion does not compare against the strongest baseline."],
            ),
        ),
    )

    evaluation = await harness.service.evaluate_detailed(
        None, None, build_probe_report(full_view(brief), rubric)
    )

    assert evaluation.attempts == 2
    retry_text = harness.spy.requests[1].messages[0].text
    assert "recommendation: finish contradicts meets_minimum=false" in retry_text
    assert harness.spy.requests[1].system_blocks == harness.spy.requests[0].system_blocks
    assert evaluation.verdict.meets_minimum is False


async def test_unscored_criterion_is_rejected_after_the_retry_budget(
    rubric: Rubric, policies: Policies
) -> None:
    """Every rubric criterion must be scored; two retries is the whole allowance."""
    brief = brief_for(ArticleType.RESEARCH)
    partial = dict(HIGH_SCORES)
    del partial["limitations_and_ethics"]
    script = Script()
    for index in range(MAX_PLANNER_RETRIES + 1):
        script = script.json_reply("planner", index, evaluation_payload(scores=partial))
    harness = make_service(rubric=rubric, policies=policies, brief=brief, script=script)

    with pytest.raises(EvaluationRejected) as caught:
        await harness.service.evaluate_detailed(
            None, None, build_probe_report(full_view(brief), rubric)
        )

    assert caught.value.attempts == MAX_PLANNER_RETRIES + 1
    assert "scores: missing ['limitations_and_ethics']" in " | ".join(caught.value.problems)
    assert harness.provider.call_count == MAX_PLANNER_RETRIES + 1


async def test_evaluation_is_bounded_and_carries_no_article_body(
    rubric: Rubric, policies: Policies
) -> None:
    """The payload is the compact view: gates, counts and edge sentences, not the article."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script().json_reply("planner", 0, evaluation_payload()),
    )
    # A sentinel buried deep in a long section: the compact view cannot reach it.
    long_body = " ".join(f"word{index}" for index in range(2_000)) + " DEEP-MARKER"
    view = article_view(brief, texts={"methodology": long_body})
    probes = build_probe_report(view, rubric)

    await harness.service.evaluate_detailed(None, None, probes, deep_check=False)

    request = harness.spy.requests[0]
    message = request.messages[0].text
    assert "## Hard gates (deterministic; you cannot change these)" in message
    assert "## Compact article view" in message
    assert "DEEP-MARKER" not in message
    assert "## Full text" not in message
    # The view reports counts and edge sentences, which is what the evaluator scores.
    assert '"words":800' in message
    assert "Methodology (methodology)" in message
    assert request.max_output_tokens == EVALUATOR_MAX_OUTPUT_TOKENS
    prefix = "\n\n".join(block.text for block in request.system_blocks)
    assert "# Evaluator" in prefix
    assert "# Planner" not in prefix
    prompt = harness.service.last_prompt
    assert prompt is not None
    assert prompt.input_tokens < 4_000


async def test_deep_check_is_sent_once_then_replaced_by_a_note(
    rubric: Rubric, policies: Policies
) -> None:
    """Opt-in full text is bounded and never re-sent unchanged: that is the token rule."""
    brief = brief_for(ArticleType.RESEARCH)
    harness = make_service(
        rubric=rubric,
        policies=policies,
        brief=brief,
        script=Script()
        .json_reply("planner", 0, evaluation_payload())
        .json_reply("planner", 1, evaluation_payload()),
    )
    body = "The bounded full text lives here and nowhere else. Second sentence follows."
    probes = build_probe_report(
        article_view(brief, texts={"conclusion": body}), rubric, deep_check=["conclusion"]
    )

    first = await harness.service.evaluate_detailed(None, None, probes)
    second = await harness.service.evaluate_detailed(None, None, probes)

    first_text = harness.spy.requests[0].messages[0].text
    second_text = harness.spy.requests[1].messages[0].text
    assert "## Full text (opt-in, bounded)" in first_text
    assert body in first_text.split("## Full text (opt-in, bounded)")[1]
    assert "unchanged since the previous evaluation" in second_text
    assert body not in second_text.split("## Full text (opt-in)")[1]
    assert first.deep_checked == ("conclusion",)
    assert second.deep_checked == ("conclusion",)


async def test_evaluate_needs_a_brief(rubric: Rubric, policies: Policies) -> None:
    """Without a brief there is nothing to evaluate against."""
    harness = make_service(rubric=rubric, policies=policies)
    probes = build_probe_report(full_view(brief_for(ArticleType.RESEARCH)), rubric)

    with pytest.raises(PlannerStateMissing):
        await harness.service.evaluate(None, None, probes)


def test_merge_only_ever_gets_stricter(rubric: Rubric, policies: Policies) -> None:
    """Directly: a model that says 'done' cannot survive a failed gate or a low Q."""
    brief = brief_for(ArticleType.RESEARCH)
    draft = EvaluationDraft.model_validate(evaluation_payload())
    good = build_probe_report(full_view(brief), rubric)
    bad = build_probe_report(failing_view(rubric), rubric)

    assert merge_verdict(draft=draft, probes=good, policies=policies, rubric=rubric).meets_minimum
    overruled = merge_verdict(draft=draft, probes=bad, policies=policies, rubric=rubric)
    assert overruled.meets_minimum is False
    blocked = merge_verdict(
        draft=draft, probes=good, policies=policies, rubric=rubric, report_high_issues=1
    )
    assert blocked.meets_minimum is False


def test_verdict_recommendation_is_forced_to_finish_when_nothing_is_missing(
    rubric: Rubric, policies: Policies
) -> None:
    """A 'continue' with an empty target list would iterate for free, so it becomes finish."""
    brief = brief_for(ArticleType.RESEARCH)
    draft = EvaluationDraft.model_validate(
        evaluation_payload(recommendation=Recommendation.CONTINUE.value, missing=["Something"])
    )

    verdict = merge_verdict(
        draft=draft,
        probes=build_probe_report(full_view(brief), rubric),
        policies=policies,
        rubric=rubric,
    )

    assert verdict.meets_minimum is True
    assert verdict.recommendation is Recommendation.CONTINUE
    assert verdict.missing == ["Something"]


def test_weighted_q_follows_rubric_weights(rubric: Rubric) -> None:
    """Q is auditable: rubric weights, integer scale, half-up rounding."""
    perfect = {criterion.name: 100 for criterion in rubric.criteria}
    zero = {criterion.name: 0 for criterion in rubric.criteria}
    mixed = {criterion.name: 50 for criterion in rubric.criteria}

    assert weighted_q(perfect, rubric) == 100
    assert weighted_q(zero, rubric) == 0
    assert weighted_q(mixed, rubric) == 50

    with pytest.raises(KeyError):
        weighted_q({"coherence": 90}, rubric)


def test_evaluation_problems_are_specific(rubric: Rubric) -> None:
    """Missing criterion, invented criterion and contradictory recommendation, in order."""
    invented = dict(HIGH_SCORES)
    invented["novelty"] = 90
    problems = evaluation_problems(
        EvaluationDraft.model_validate(evaluation_payload(scores=invented)), rubric=rubric
    )
    assert any("unknown criteria ['novelty']" in problem for problem in problems)

    contradictory = evaluation_problems(
        EvaluationDraft.model_validate(
            evaluation_payload(meets_minimum=False, recommendation=Recommendation.FINISH.value)
        ),
        rubric=rubric,
    )
    assert any("finish contradicts meets_minimum=false" in problem for problem in contradictory)

    untargeted = evaluation_problems(
        EvaluationDraft.model_validate(
            evaluation_payload(recommendation=Recommendation.CONTINUE.value)
        ),
        rubric=rubric,
    )
    assert any("needs at least one concrete requirement" in problem for problem in untargeted)


def test_a_clean_draft_has_no_problems(rubric: Rubric) -> None:
    """The happy path is silence, so a retry is never paid for nothing."""
    draft = EvaluationDraft.model_validate(evaluation_payload())

    assert evaluation_problems(draft, rubric=rubric) == []


def test_loop_decision_is_accepted_by_replan() -> None:
    """Replanning reads the loop's decision, not a prose summary of it."""
    decision = LoopDecision(
        iter=3,
        action=LoopAction.CONTINUE,
        reasons=["plateau_detected"],
        evidence={"plateau_count": 1},
    )

    assert decision.action is LoopAction.CONTINUE
    assert decision.reasons == ["plateau_detected"]
    assert decision.needs_planner is True
