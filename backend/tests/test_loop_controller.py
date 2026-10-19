"""LoopController tests: pure decisions over numbers (no provider at all).

The precedence is the contract here: user > needs-input > errors > acceptable
quality > budget > erosion > continue. Each detector also gets its own
insufficient-data cases, because an erosion stop on the first iteration would
be worse than no detector at all.
"""

from __future__ import annotations

import pytest

from app.contracts.enums import IssueSeverity, LoopAction
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.contracts.verdict import Verdict
from app.core.policies import DEFAULT_POLICIES, DiminishingPolicy, Policies
from app.orchestrator.controller import (
    LoopController,
    LoopState,
    churn_detected,
    detect_erosion,
    diminishing_detected,
    oscillation_detected,
    plateau_detected,
    regression_detected,
)


def make_report(
    *,
    q: int = 85,
    iteration: int = 1,
    failed: tuple[str, ...] = (),
    high_issues: int = 0,
) -> QualityReport:
    gates = {name: name not in failed for name in GATE_NAMES}
    issues = [
        QualityIssue(
            id=f"ISSUE-test-{index:03d}",
            section_id="introduction",
            severity=IssueSeverity.HIGH,
            text="A blocking defect for the test.",
        )
        for index in range(high_issues)
    ]
    return QualityReport(
        iter=iteration, gates=gates, Q=q, verified_citation_ratio=1.0, issues=issues
    )


def finish_verdict() -> Verdict:
    return Verdict(meets_minimum=True, recommendation="finish")


def continue_verdict(*, meets: bool = False) -> Verdict:
    return Verdict(meets_minimum=meets, missing=["Close the open gate"], recommendation="continue")


def decide(state: LoopState, policies: Policies = DEFAULT_POLICIES):
    return LoopController(policies).decide(state)


def good_state(**overrides: object) -> LoopState:
    """An acceptable article at iteration 2; tests break one fact at a time."""
    values: dict[str, object] = {
        "iteration": 2,
        "q_history": (60.0, 85.0),
        "iteration_tokens": (1_000, 1_000),
        "run_tokens": 2_000,
        "report": make_report(),
        "verdict": finish_verdict(),
    }
    values.update(overrides)
    return LoopState(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- precedence
def test_user_stop_beats_everything() -> None:
    decision = decide(good_state(user_stop=True, needs_user_input=True, error_streak=99))
    assert decision.action is LoopAction.STOP_USER
    assert decision.reasons == ["user_requested_stop"]
    assert decision.is_terminal


def test_needs_user_input_pauses_even_when_quality_is_acceptable() -> None:
    decision = decide(good_state(needs_user_input=True, error_streak=99))
    assert decision.action is LoopAction.ASK_USER
    assert decision.reasons == ["needs_user_input"]
    assert not decision.is_terminal  # ASK_USER pauses, it does not stop


def test_error_streak_stops_before_quality_is_considered() -> None:
    decision = decide(good_state(error_streak=DEFAULT_POLICIES.error_streak))
    assert decision.action is LoopAction.STOP_ERRORS
    assert "error_streak_reached" in decision.reasons
    assert decision.evidence["error_streak"] == DEFAULT_POLICIES.error_streak


def test_acceptable_quality_is_offered() -> None:
    decision = decide(good_state())
    assert decision.action is LoopAction.OFFER_FINISH
    assert decision.reasons == ["quality_acceptable"]
    assert decision.evidence["q_latest"] == 85.0
    assert decision.evidence["failed_gates"] == []


def test_planner_can_delay_but_not_create_an_offer() -> None:
    # Planner says the minimum is unmet: no offer from numbers alone... but the
    # reverse also holds - a finish verdict cannot rescue failing gates.
    delayed = decide(good_state(verdict=continue_verdict()))
    assert delayed.action is LoopAction.CONTINUE
    assert "planner_reports_missing_work" in delayed.reasons

    failing = decide(good_state(report=make_report(failed=("no_placeholders",))))
    assert failing.action is LoopAction.CONTINUE
    assert "gates_failing" in failing.reasons


def test_high_severity_issue_blocks_the_offer() -> None:
    decision = decide(good_state(report=make_report(high_issues=1)))
    assert decision.action is LoopAction.CONTINUE
    assert "high_severity_issues_open" in decision.reasons


def test_q_below_minimum_is_never_offered() -> None:
    decision = decide(good_state(report=make_report(q=DEFAULT_POLICIES.q_min - 1), verdict=None))
    assert decision.action is LoopAction.CONTINUE
    assert "quality_below_minimum" in decision.reasons


def test_acceptable_quality_beats_budget_and_erosion_stops() -> None:
    # Q plateaued at an acceptable level and the budget is gone: offer, with
    # the reasons recorded instead of a bare STOP.
    state = good_state(
        verdict=continue_verdict(meets=True),
        q_history=(84.0, 85.0, 85.0),
        iteration_tokens=(1_000, 1_000, 1_000),
        run_tokens=DEFAULT_POLICIES.run_token_cap,
    )
    decision = decide(state)
    assert decision.action is LoopAction.OFFER_FINISH
    assert decision.reasons[0] == "quality_acceptable"
    assert "run_token_cap_reached" in decision.reasons


def test_budget_stops_an_unfinished_article() -> None:
    state = good_state(report=make_report(q=40), verdict=continue_verdict(), run_tokens=999_999_999)
    decision = decide(state)
    assert decision.action is LoopAction.STOP_BUDGET
    assert "run_token_cap_reached" in decision.reasons
    assert decision.evidence["run_tokens"] == 999_999_999


def test_max_iterations_is_a_budget_stop() -> None:
    policies = Policies(max_iterations=2)
    state = good_state(report=make_report(q=40), verdict=continue_verdict())
    decision = decide(state, policies)
    assert decision.action is LoopAction.STOP_BUDGET
    assert "max_iterations_reached" in decision.reasons


def test_iteration_cap_is_a_budget_stop() -> None:
    state = good_state(
        report=make_report(q=40),
        verdict=continue_verdict(),
        iteration_tokens=(1_000, DEFAULT_POLICIES.iteration_token_cap),
    )
    decision = decide(state)
    assert decision.action is LoopAction.STOP_BUDGET
    assert "iteration_token_cap_reached" in decision.reasons


def test_no_report_continues_with_an_explicit_reason() -> None:
    decision = decide(LoopState(iteration=1, run_tokens=500))
    assert decision.action is LoopAction.CONTINUE
    assert decision.reasons == ["no_quality_report"]
    assert decision.evidence["q_latest"] is None


def test_regression_is_an_erosion_stop() -> None:
    state = good_state(
        report=make_report(q=55, failed=("no_placeholders",)),
        verdict=continue_verdict(),
        q_history=(80.0, 55.0),
    )
    decision = decide(state)
    assert decision.action is LoopAction.STOP_EROSION
    assert "quality_regression_detected" in decision.reasons


def test_recurred_section_is_an_erosion_stop() -> None:
    state = good_state(
        report=make_report(q=55, failed=("no_placeholders",)),
        verdict=continue_verdict(),
        q_history=(60.0, 55.0),
        recurred_sections=("introduction",),
    )
    decision = decide(state)
    assert decision.action is LoopAction.STOP_EROSION
    assert decision.reasons[0] == "section_hash_recurred"
    assert decision.evidence["recurred_sections"] == ["introduction"]


def test_every_decision_carries_auditable_evidence() -> None:
    decision = decide(good_state())
    for key in ("q_history", "run_tokens", "run_token_cap", "failed_gates", "erosion_detectors"):
        assert key in decision.evidence
    assert all(reason == reason.lower() and " " not in reason for reason in decision.reasons)


# --------------------------------------------------------------------------- detectors
@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((50.0, 60.0, 70.0), False),  # still climbing
        ((50.0, 50.4, 50.8), True),  # 0.8 gain over the window < 1.0
        ((50.0, 60.0), False),  # not enough measurements for a window of 3
        ((70.0, 60.0, 69.0), True),  # round-tripped back under the gain bound
    ],
)
def test_plateau_detector(history: tuple[float, ...], expected: bool) -> None:
    assert plateau_detected(history, window=3, min_gain=1.0) is expected


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((80.0, 76.0), True),  # -4 within delta 3
        ((80.0, 78.0), False),  # -2 is noise
        ((80.0,), False),  # one point cannot regress
        ((50.0, 80.0, 79.0, 60.0), True),  # vs best of the preceding window
    ],
)
def test_regression_detector(history: tuple[float, ...], expected: bool) -> None:
    assert regression_detected(history, window=2, delta=3.0) is expected


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((50.0, 60.0, 55.0, 65.0), True),  # up, down, up
        ((50.0, 60.0, 55.0, 54.0), False),  # last move keeps falling
        ((50.0, 50.0, 60.0, 55.0), False),  # a flat step is not alternation
        ((50.0, 60.0, 55.0), False),  # too few points for a window of 4
    ],
)
def test_oscillation_detector(history: tuple[float, ...], expected: bool) -> None:
    assert oscillation_detected(history, window=4) is expected


def test_diminishing_detector() -> None:
    policy = DiminishingPolicy(min_gain_per_10k=0.2, window=2)
    # Two iterations, 50k tokens each, +0.5 Q each: 0.1 per 10k < 0.2.
    assert diminishing_detected((50.0, 50.5, 51.0), (50_000, 50_000), policy)
    # Same spend, real progress in the second iteration.
    assert not diminishing_detected((50.0, 50.5, 53.0), (50_000, 50_000), policy)
    # Not enough history, and an unspent iteration proves nothing.
    assert not diminishing_detected((50.0, 50.5), (50_000,), policy)
    assert not diminishing_detected((50.0, 50.5, 51.0), (50_000, 0), policy)


def test_churn_detector_needs_a_sample_before_it_blames_the_loop() -> None:
    assert not churn_detected(churn_ratio=1.0, threshold=0.3, total_opened=2, measured_iterations=5)
    assert not churn_detected(churn_ratio=1.0, threshold=0.3, total_opened=9, measured_iterations=1)
    assert churn_detected(churn_ratio=0.9, threshold=0.3, total_opened=9, measured_iterations=3)
    assert not churn_detected(churn_ratio=0.2, threshold=0.3, total_opened=9, measured_iterations=3)


def test_detect_erosion_reports_codes_in_order() -> None:
    state = LoopState(
        iteration=4,
        q_history=(80.0, 82.0, 81.0, 60.0),
        iteration_tokens=(1_000, 1_000, 1_000, 1_000),
        recurred_sections=("results",),
    )
    reasons = detect_erosion(state, DEFAULT_POLICIES)
    assert reasons[0] == "section_hash_recurred"
    assert "quality_regression_detected" in reasons
