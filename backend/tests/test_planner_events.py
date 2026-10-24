"""The event policy in front of ``plan()``: when a planner call is allowed at all.

The policy is the cheapest possible defence of the token budget, so it is tested as a
state machine: each event, each guard, and each "this must not spend another call" case.
"""

from __future__ import annotations

import pytest

from app.contracts.enums import LoopAction
from app.contracts.loop import LoopDecision
from app.planner import PLATEAU_REASON, PlannerCallPolicy, PlannerEvent


def decision(
    *,
    action: LoopAction = LoopAction.CONTINUE,
    reasons: list[str] | None = None,
    **evidence: object,
) -> LoopDecision:
    """A loop decision with one stable reason code."""
    return LoopDecision(
        iter=int(evidence.pop("iter", 1)),
        action=action,
        reasons=reasons or ["quality_below_target"],
        evidence=dict(evidence),
    )


def test_run_start_is_allowed_only_before_the_first_plan() -> None:
    """A run that already has a plan does not need a second first plan."""
    policy = PlannerCallPolicy()

    call = policy.should_call(PlannerEvent.RUN_START, iteration=0, plan_version=None)
    assert call is not None
    assert call.event is PlannerEvent.RUN_START
    assert call.reason == "no_plan_yet"
    assert call.strategy_change is False

    assert policy.should_call(PlannerEvent.RUN_START, iteration=1, plan_version=1) is None


def test_one_call_per_iteration() -> None:
    """The second event of the same iteration never spends a second call."""
    policy = PlannerCallPolicy()

    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=4) is not None
    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=4) is None
    assert policy.last_call_iteration == 4
    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=5) is not None


def test_minimum_iteration_gap() -> None:
    """A gap keeps the planner out of consecutive iterations when the loop asks for it."""
    policy = PlannerCallPolicy(min_iteration_gap=2)

    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=1) is not None
    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=2) is None
    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=3) is None
    assert policy.should_call(PlannerEvent.QUEUE_EMPTY, iteration=4) is not None


def test_gate_newly_failed_requires_something_new() -> None:
    """A gate that has been failing for five iterations is not news."""
    policy = PlannerCallPolicy()
    policy.observe_failed_gates(["no_placeholders"])

    assert (
        policy.should_call(
            PlannerEvent.GATE_NEWLY_FAILED, iteration=1, failed_gates=["no_placeholders"]
        )
        is None
    )

    call = policy.should_call(
        PlannerEvent.GATE_NEWLY_FAILED,
        iteration=2,
        failed_gates=["no_placeholders", "citations_resolve_both_ways"],
    )
    assert call is not None
    assert call.reason == "gate_newly_failed:citations_resolve_both_ways"

    # The granted call marks the gate as seen, so it cannot buy a second call.
    assert (
        policy.should_call(
            PlannerEvent.GATE_NEWLY_FAILED,
            iteration=3,
            failed_gates=["citations_resolve_both_ways"],
        )
        is None
    )


def test_milestone_complete_is_delivered_once() -> None:
    """A repeated milestone event is a duplicate, not a reason to replan."""
    policy = PlannerCallPolicy()

    first = policy.should_call(PlannerEvent.MILESTONE_COMPLETE, iteration=1, milestone="draft-v1")
    assert first is not None
    assert first.reason == "milestone:draft-v1"

    assert (
        policy.should_call(PlannerEvent.MILESTONE_COMPLETE, iteration=2, milestone="draft-v1")
        is None
    )
    assert policy.should_call(PlannerEvent.MILESTONE_COMPLETE, iteration=2, milestone=None) is None
    assert (
        policy.should_call(PlannerEvent.MILESTONE_COMPLETE, iteration=2, milestone="draft-v2")
        is not None
    )


def test_first_plateau_asks_for_a_strategy_change_once() -> None:
    """Only the first plateau is a planning event, and it is the one that changes strategy."""
    policy = PlannerCallPolicy()

    assert policy.should_call(PlannerEvent.FIRST_PLATEAU, iteration=1, plateau_count=2) is None
    assert policy.plateau_handled is False

    call = policy.should_call(PlannerEvent.FIRST_PLATEAU, iteration=2, plateau_count=1)
    assert call is not None
    assert call.strategy_change is True
    assert call.reason == PLATEAU_REASON
    assert policy.plateau_handled is True

    # Never re-armed: a second plateau is the loop's problem, not a fresh planning event.
    assert policy.should_call(PlannerEvent.FIRST_PLATEAU, iteration=9, plateau_count=1) is None


def test_stop_actions_never_call_the_planner() -> None:
    """A run that is stopping does not buy a plan."""
    policy = PlannerCallPolicy()

    for action in (
        LoopAction.STOP_BUDGET,
        LoopAction.STOP_ERRORS,
        LoopAction.STOP_EROSION,
        LoopAction.STOP_USER,
    ):
        assert policy.from_decision(decision(action=action)) is None
    assert policy.from_decision(decision(action=LoopAction.ASK_USER)) is None


def test_plateau_reason_wins_over_an_empty_queue() -> None:
    """One event per decision, chosen deterministically: plateau, queue, gate."""
    policy = PlannerCallPolicy()

    call = policy.from_decision(
        decision(reasons=[PLATEAU_REASON, "queue_empty"], plateau_count=1),
        plan_version=1,
        queue_empty=True,
    )

    assert call is not None
    assert call.event is PlannerEvent.FIRST_PLATEAU
    assert call.strategy_change is True


def test_plateau_count_evidence_is_read_defensively() -> None:
    """Missing or non-integer evidence falls back to 'first plateau'."""
    policy = PlannerCallPolicy()

    call = policy.from_decision(decision(reasons=[PLATEAU_REASON], plateau_count="many"))

    assert call is not None
    assert call.event is PlannerEvent.FIRST_PLATEAU


def test_empty_queue_is_a_planning_event() -> None:
    """An empty queue with work left is exactly when the plan must change."""
    policy = PlannerCallPolicy()

    call = policy.from_decision(decision(), queue_empty=True)

    assert call is not None
    assert call.event is PlannerEvent.QUEUE_EMPTY
    assert call.reason == "task_queue_empty"


def test_newly_failing_gate_is_a_planning_event() -> None:
    """A gate that just started failing is news; the same gate next iteration is not."""
    policy = PlannerCallPolicy()
    policy.observe_failed_gates(["no_placeholders"])

    call = policy.from_decision(
        decision(), failed_gates=["no_placeholders", "citation_style_matches"]
    )
    assert call is not None
    assert call.event is PlannerEvent.GATE_NEWLY_FAILED

    assert policy.from_decision(decision(iter=2), failed_gates=["no_placeholders"]) is None


def test_a_quiet_iteration_costs_nothing() -> None:
    """No event, no call: this is what keeps a 30-iteration run affordable."""
    policy = PlannerCallPolicy()

    assert policy.from_decision(decision()) is None
    assert policy.last_call_iteration is None


def test_policy_state_is_inspectable() -> None:
    """The orchestrator can log exactly what the policy has already seen."""
    policy = PlannerCallPolicy()
    policy.observe_failed_gates(["no_placeholders"])
    policy.should_call(PlannerEvent.MILESTONE_COMPLETE, iteration=1, milestone="draft-v1")

    assert policy.known_failed_gates == frozenset({"no_placeholders"})
    assert policy.last_call_iteration == 1
    assert policy.plateau_handled is False


def test_invalid_configuration_is_rejected() -> None:
    """Bad policy inputs fail loudly instead of silently changing the call rate."""
    with pytest.raises(ValueError, match="min_iteration_gap"):
        PlannerCallPolicy(min_iteration_gap=-1)
    with pytest.raises(ValueError, match="iteration"):
        PlannerCallPolicy().should_call(PlannerEvent.QUEUE_EMPTY, iteration=-1)
