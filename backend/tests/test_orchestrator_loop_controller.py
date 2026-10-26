"""Loop controller: every detector at threshold ±1, precedence, strikes,
and the termination property (any report sequence stops in max_iterations).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from app.contracts.enums import ChangeStatus, LoopAction, Recommendation
from app.contracts.ledger import TokenUsage
from app.contracts.loop import STOP_ACTIONS
from app.contracts.verdict import Verdict
from app.core.policies import Policies
from app.orchestrator.loop_controller import (
    IterationRecord,
    LoopHistory,
    detect_caps,
    detect_completion,
    detect_diminishing_returns,
    detect_error_streak,
    detect_integrity_drift,
    detect_issue_churn,
    detect_oscillation,
    detect_plateau,
    detect_regression,
    evaluate,
    next_strikes,
)
from tests.orchestrator_support import make_entry, make_report

pytestmark = pytest.mark.orchestrator

POLICIES = Policies()


def history_of(qs: list[int], **kwargs: object) -> LoopHistory:
    """History from a Q series (gates failing so completion stays out)."""
    gates_pass = bool(kwargs.pop("gates_pass", False))
    records = [
        IterationRecord(iter=index + 1, report=make_report(index + 1, q, gates_pass=gates_pass))
        for index, q in enumerate(qs)
    ]
    return LoopHistory(records=records, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- user & caps
def test_user_stop_beats_everything() -> None:
    history = history_of([95], gates_pass=True, user_stop_requested=True)
    decision = evaluate(history, POLICIES)
    assert decision.action is LoopAction.STOP_USER
    assert decision.reasons == ["user_stop_requested"]


def test_iteration_cap_at_threshold() -> None:
    under = LoopHistory(records=[IterationRecord(iter=POLICIES.max_iterations - 1)])
    over = LoopHistory(records=[IterationRecord(iter=POLICIES.max_iterations)])
    assert detect_caps(under, POLICIES) is None
    hit = detect_caps(over, POLICIES)
    assert hit is not None and hit.reason == "max_iterations_reached"
    assert evaluate(over, POLICIES).action is LoopAction.STOP_BUDGET


def test_token_cap_at_threshold() -> None:
    under = LoopHistory(records=[IterationRecord(iter=1)], tokens_spent=POLICIES.run_token_cap - 1)
    over = LoopHistory(records=[IterationRecord(iter=1)], tokens_spent=POLICIES.run_token_cap)
    assert detect_caps(under, POLICIES) is None
    assert detect_caps(over, POLICIES).reason == "run_token_cap_reached"  # type: ignore[union-attr]


def test_wall_and_account_caps() -> None:
    wall = LoopHistory(
        records=[IterationRecord(iter=1)], wall_seconds=100.0, max_wall_seconds=100.0
    )
    assert detect_caps(wall, POLICIES).reason == "wall_clock_cap_reached"  # type: ignore[union-attr]
    account = LoopHistory(
        records=[IterationRecord(iter=1)], account_budget_tokens=50, account_tokens_spent=50
    )
    assert detect_caps(account, POLICIES).reason == "account_budget_exhausted"  # type: ignore[union-attr]
    account_under = LoopHistory(
        records=[IterationRecord(iter=1)], account_budget_tokens=50, account_tokens_spent=49
    )
    assert detect_caps(account_under, POLICIES) is None


# --------------------------------------------------------------------------- errors & input
def test_error_streak_at_threshold() -> None:
    failed = [make_entry(status=ChangeStatus.FAILED) for _ in range(POLICIES.error_streak)]
    under = LoopHistory(records=[IterationRecord(iter=1, entries=failed[:-1])])
    over = LoopHistory(records=[IterationRecord(iter=1, entries=failed)])
    assert detect_error_streak(under, POLICIES) is None
    assert detect_error_streak(over, POLICIES) is not None
    assert evaluate(over, POLICIES).action is LoopAction.STOP_ERRORS
    broken = LoopHistory(records=[IterationRecord(iter=1, entries=[*failed, make_entry()])])
    assert detect_error_streak(broken, POLICIES) is None


def test_needs_user_input_asks_user() -> None:
    entries = [make_entry(status=ChangeStatus.NEEDS_USER_INPUT)]
    history = LoopHistory(records=[IterationRecord(iter=1, entries=entries)])
    decision = evaluate(history, POLICIES)
    assert decision.action is LoopAction.ASK_USER
    assert decision.evidence["blocked_entry_ids"] == [entries[0].id]


# --------------------------------------------------------------------------- soft detectors
def test_plateau_at_threshold() -> None:
    # window=3, min_gain=1.0: gain 0 fires, gain 1 does not.
    assert detect_plateau(history_of([50, 50, 50]), POLICIES) is not None
    assert detect_plateau(history_of([50, 50, 51]), POLICIES) is None
    # too little history never fires
    assert detect_plateau(history_of([50, 50]), POLICIES) is None


def test_regression_q_drop_at_threshold() -> None:
    delta = int(POLICIES.regression.delta)
    assert detect_regression(history_of([60, 60 - (delta - 1)]), POLICIES) is None
    hit = detect_regression(history_of([60, 60 - delta]), POLICIES)
    assert hit is not None and hit.reason == "q_regressed"


def test_regression_gate_relapse() -> None:
    first = make_report(1, 60, gates_pass=True)
    second = make_report(2, 60, failed_gate="no_placeholders")
    history = LoopHistory(
        records=[IterationRecord(iter=1, report=first), IterationRecord(iter=2, report=second)]
    )
    hit = detect_regression(history, POLICIES)
    assert hit is not None and hit.reason == "gate_regressed"
    assert hit.evidence["regressed_gates"] == ["no_placeholders"]


def test_oscillation_at_threshold() -> None:
    assert detect_oscillation(history_of([50, 60, 50]), POLICIES) is None  # window-1
    assert detect_oscillation(history_of([50, 60, 50, 60]), POLICIES) is not None
    assert detect_oscillation(history_of([50, 60, 70, 60]), POLICIES) is None  # not alternating
    assert detect_oscillation(history_of([50, 60, 50, 50]), POLICIES) is None  # flat step


def test_integrity_drift_ratio_and_count() -> None:
    steady = LoopHistory(
        records=[
            IterationRecord(iter=1, report=make_report(1, 50, ratio=0.8)),
            IterationRecord(iter=2, report=make_report(2, 55, ratio=0.8)),
        ]
    )
    assert detect_integrity_drift(steady) is None
    fell = LoopHistory(
        records=[
            IterationRecord(iter=1, report=make_report(1, 50, ratio=0.8)),
            IterationRecord(iter=2, report=make_report(2, 55, ratio=0.7)),
        ]
    )
    hit = detect_integrity_drift(fell)
    assert hit is not None and hit.reason == "verified_citation_ratio_fell"
    grew = LoopHistory(
        records=[
            IterationRecord(iter=1, unverified_citations=0),
            IterationRecord(iter=2, unverified_citations=1),
        ]
    )
    hit = detect_integrity_drift(grew)
    assert hit is not None and hit.reason == "unverified_references_appeared"


def test_diminishing_returns_at_threshold() -> None:
    # min 0.2 Q per 10k tokens, window 2. 10k tokens/iter: +1 Q -> rate 1.0.
    def record(iteration: int, q: int, tokens: int) -> IterationRecord:
        return IterationRecord(
            iter=iteration,
            report=make_report(iteration, q),
            tokens=TokenUsage(in_=tokens, out=0, cached=0),
        )

    healthy = LoopHistory(records=[record(1, 50, 0), record(2, 51, 10_000), record(3, 52, 10_000)])
    assert detect_diminishing_returns(healthy, POLICIES) is None
    # 100k tokens per iteration for +1 Q -> rate 0.1 < 0.2 on both intervals.
    burning = LoopHistory(
        records=[record(1, 50, 0), record(2, 51, 100_000), record(3, 52, 100_000)]
    )
    assert detect_diminishing_returns(burning, POLICIES) is not None
    # only one measurable interval: never fires
    short = LoopHistory(records=[record(1, 50, 0), record(2, 51, 100_000)])
    assert detect_diminishing_returns(short, POLICIES) is None
    # zero-token iterations are skipped, not judged
    free = LoopHistory(records=[record(1, 50, 0), record(2, 50, 0), record(3, 50, 0)])
    assert detect_diminishing_returns(free, POLICIES) is None


def test_issue_churn_at_threshold() -> None:
    issue_ids = [f"ISSUE-{index:03d}" for index in range(10)]

    def churn_history(still_open: int) -> LoopHistory:
        opened = make_entry(iteration=1, opened=issue_ids)
        closed = make_entry(iteration=2, closed=issue_ids[: 10 - still_open])
        return LoopHistory(
            records=[
                IterationRecord(iter=1, entries=[opened]),
                IterationRecord(iter=2, entries=[closed]),
            ]
        )

    # ratio exactly 0.3 -> no hit; 0.4 -> hit
    assert detect_issue_churn(churn_history(3), POLICIES) is None
    assert detect_issue_churn(churn_history(4), POLICIES) is not None


# --------------------------------------------------------------------------- completion
def test_completion_requires_gates_and_qmin() -> None:
    below = history_of([POLICIES.q_min - 1], gates_pass=True)
    assert detect_completion(below, POLICIES) is None
    failing = history_of([90], gates_pass=False)
    assert detect_completion(failing, POLICIES) is None
    ready = history_of([POLICIES.q_min], gates_pass=True)
    hit = detect_completion(ready, POLICIES)
    assert hit is not None and hit.evidence["needs_planner_evaluation"] is True
    assert evaluate(ready, POLICIES).action is LoopAction.OFFER_FINISH


def test_completion_verdict_veto_and_agreement() -> None:
    finish = Verdict(meets_minimum=True, missing=[], risks=[], recommendation=Recommendation.FINISH)
    cont = Verdict(
        meets_minimum=False, missing=["More work"], risks=[], recommendation=Recommendation.CONTINUE
    )
    agreed = history_of([90], gates_pass=True, verdict=finish)
    hit = detect_completion(agreed, POLICIES)
    assert hit is not None and hit.evidence["needs_planner_evaluation"] is False
    vetoed = history_of([90], gates_pass=True, verdict=cont)
    assert detect_completion(vetoed, POLICIES) is None


# --------------------------------------------------------------------------- strikes & precedence
def test_two_strikes_stop_with_replan_first() -> None:
    history = history_of([50, 50, 50])
    first = evaluate(history, POLICIES)
    assert first.action is LoopAction.CONTINUE
    assert first.reasons == ["plateau_detected", "strategy_change"]
    assert first.evidence["strategy_change"] is True
    history = history.model_copy(update={"strikes": next_strikes(history, first)})
    history.records.append(IterationRecord(iter=4, report=make_report(4, 50)))
    second = evaluate(history, POLICIES)
    assert second.action is LoopAction.STOP_EROSION
    assert "second_strike" in second.reasons


def test_strikes_forgiven_on_new_best() -> None:
    history = history_of([50, 50, 50])
    first = evaluate(history, POLICIES)
    strikes = next_strikes(history, first)
    assert strikes == {"plateau": 1}
    improved = history_of([50, 50, 50, 65], strikes=strikes)
    decision = evaluate(improved, POLICIES)
    assert decision.action is LoopAction.CONTINUE
    assert next_strikes(improved, decision) == {}


def test_precedence_caps_over_erosion_and_completion() -> None:
    history = history_of([50, 50, 50], tokens_spent=POLICIES.run_token_cap)
    assert evaluate(history, POLICIES).action is LoopAction.STOP_BUDGET
    record = IterationRecord(
        iter=POLICIES.max_iterations, report=make_report(1, 95, gates_pass=True)
    )
    complete_but_capped = LoopHistory(records=[record])
    assert evaluate(complete_but_capped, POLICIES).action is LoopAction.STOP_BUDGET


def test_precedence_soft_over_completion() -> None:
    # Q regressed into the latest iteration even though gates pass and Q >= q_min.
    history = history_of([95, 80], gates_pass=True)
    decision = evaluate(history, POLICIES)
    assert decision.action is LoopAction.CONTINUE
    assert decision.evidence["detector"] == "regression"


def test_every_decision_carries_evidence() -> None:
    for history in (LoopHistory(), history_of([40]), history_of([90], gates_pass=True)):
        decision = evaluate(history, POLICIES)
        for key in ("q_series", "tokens_spent", "best_q", "failed_gates", "detector"):
            assert key in decision.evidence


# --------------------------------------------------------------------------- property
_report_strategy = st.fixed_dictionaries(
    {
        "q": st.integers(min_value=0, max_value=100),
        "gates_pass": st.booleans(),
        "ratio": st.floats(min_value=0.0, max_value=1.0),
        "tokens": st.integers(min_value=0, max_value=200_000),
        "failed_entries": st.integers(min_value=0, max_value=2),
        "needs_input": st.booleans(),
    }
)


@hypothesis_settings(max_examples=120, deadline=None)
@given(
    reports=st.lists(_report_strategy, min_size=0, max_size=30),
    user_stop=st.booleans(),
    verdict_finish=st.booleans(),
)
def test_any_report_sequence_terminates_within_max_iterations(
    reports: list[dict[str, object]], user_stop: bool, verdict_finish: bool
) -> None:
    """Simulate the loop: feed one record per iteration, obey the controller.

    The loop ends on a terminal action, or pauses on OFFER_FINISH/ASK_USER.
    In every case it must do so with ``iter <= max_iterations`` - the cap
    fires on iteration count alone, whatever the reports contain.
    """
    verdict = (
        Verdict(meets_minimum=True, missing=[], risks=[], recommendation=Recommendation.FINISH)
        if verdict_finish
        else Verdict(
            meets_minimum=False,
            missing=["more"],
            risks=[],
            recommendation=Recommendation.CONTINUE,
        )
    )
    history = LoopHistory(user_stop_requested=user_stop, verdict=verdict)
    index = 0
    while True:
        spec = (
            reports[index]
            if index < len(reports)
            else {
                "q": 0,
                "gates_pass": False,
                "ratio": 0.0,
                "tokens": 0,
                "failed_entries": 0,
                "needs_input": False,
            }
        )
        index += 1
        entries = [
            make_entry(iteration=index, status=ChangeStatus.FAILED)
            for _ in range(int(spec["failed_entries"]))  # type: ignore[arg-type]
        ]
        if spec["needs_input"]:
            entries.append(make_entry(iteration=index, status=ChangeStatus.NEEDS_USER_INPUT))
        history.records.append(
            IterationRecord(
                iter=index,
                report=make_report(
                    index,
                    int(spec["q"]),  # type: ignore[arg-type]
                    gates_pass=bool(spec["gates_pass"]),
                    ratio=float(spec["ratio"]),  # type: ignore[arg-type]
                ),
                entries=entries,
                tokens=TokenUsage(in_=int(spec["tokens"]), out=0, cached=0),  # type: ignore[arg-type]
            )
        )
        decision = evaluate(history, POLICIES)
        history = history.model_copy(update={"strikes": next_strikes(history, decision)})
        assert decision.iter == index
        assert index <= POLICIES.max_iterations, "loop ran past max_iterations without stopping"
        if decision.action in STOP_ACTIONS or decision.action in {
            LoopAction.OFFER_FINISH,
            LoopAction.ASK_USER,
        }:
            break
        assert decision.action is LoopAction.CONTINUE
