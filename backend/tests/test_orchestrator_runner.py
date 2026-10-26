"""Runner: full loop flows - completion checkpoint, erosion stops, user
decisions, quick edits, rollback, budget revival, planner outages."""

from __future__ import annotations

import pytest

from app.contracts.enums import ChangeStatus, LoopAction, Recommendation
from app.contracts.verdict import Verdict
from app.core.policies import Policies
from app.orchestrator.errors import IntakeNeedsInput, RunConflict
from app.orchestrator.persistence import InMemoryRunStore
from app.orchestrator.runner import Orchestrator, OrchestratorConfig
from app.orchestrator.state_machine import CheckpointKind, RunStateName
from app.orchestrator.stop import OnComplete, StopOption, UserDecision
from tests.orchestrator_support import BRIEF_PAYLOAD, ScriptedAgents, ScriptedPlanner, brief

pytestmark = pytest.mark.orchestrator

FAST = OrchestratorConfig(backoff_base_s=0.0, planner_backoff_s=0.0)


def build(
    qs: tuple[int, ...] = (85,),
    *,
    planner: ScriptedPlanner | None = None,
    agents: ScriptedAgents | None = None,
    policies: Policies | None = None,
) -> Orchestrator:
    return Orchestrator(
        planner or ScriptedPlanner(),
        agents or ScriptedAgents(qs),
        policies=policies or Policies(),
        store=InMemoryRunStore(),
        config=FAST,
    )


async def test_intake_refuses_incomplete_brief() -> None:
    orchestrator = build()
    payload = dict(BRIEF_PAYLOAD)
    payload.pop("target_words")
    with pytest.raises(IntakeNeedsInput) as raised:
        orchestrator.create_run("proj", payload)
    assert "target_words" in raised.value.result.missing_fields


async def test_improving_run_reaches_completion_checkpoint_then_finish() -> None:
    planner = ScriptedPlanner()
    orchestrator = build((55, 85), planner=planner)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)

    assert record.state is RunStateName.CHECKPOINT
    assert record.checkpoint_kind is CheckpointKind.COMPLETION
    assert record.q_series() == [55, 85]
    assert planner.evaluate_calls == 1  # Planner.evaluate before offering finish
    assert record.verdict is not None and record.verdict.meets_minimum
    assert record.stop_report is not None
    assert record.stop_report.best_snapshot_id == record.best_snapshot_id
    assert StopOption.FINISH in record.stop_report.options

    record = await orchestrator.apply_decision(record.id, UserDecision(option=StopOption.FINISH))
    assert record.state is RunStateName.DONE
    # Terminal runs are returned unchanged by run() (idempotent resume).
    assert (await orchestrator.run(record.id)).state is RunStateName.DONE


async def test_on_complete_finish_skips_checkpoint() -> None:
    orchestrator = build((85,))
    record = orchestrator.create_run("proj", brief(), on_complete=OnComplete.FINISH)
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.DONE


async def test_on_complete_continue_once_runs_one_more_iteration() -> None:
    orchestrator = build((85, 88))
    record = orchestrator.create_run("proj", brief(), on_complete=OnComplete.CONTINUE_ONCE)
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.CHECKPOINT
    assert record.q_series() == [85, 88]
    assert record.continue_once_used is True


async def test_verdict_veto_replans_instead_of_finishing() -> None:
    veto = Verdict(
        meets_minimum=False,
        missing=["Still not good enough"],
        risks=[],
        recommendation=Recommendation.CONTINUE,
    )
    planner = ScriptedPlanner(verdict=veto)
    # Low Q afterwards => the run keeps iterating until the plateau strike-out.
    orchestrator = build((85, 50, 50, 50, 50), planner=planner)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)
    assert planner.evaluate_calls >= 1
    assert record.state is not RunStateName.DONE
    assert any(call is False for call in planner.replan_calls)


async def test_plateau_two_strikes_stop_with_report_and_strategy_change() -> None:
    planner = ScriptedPlanner()
    orchestrator = build((50, 50, 50, 50), planner=planner)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)

    assert record.state is RunStateName.STOPPED_EROSION
    assert True in planner.replan_calls  # strike 1 replanned with strategy_change
    assert record.stop_report is not None
    assert record.stop_report.action is LoopAction.STOP_EROSION
    assert record.stop_report.q_series == [50, 50, 50, 50]
    assert StopOption.ROLLBACK in record.stop_report.options
    assert StopOption.CONTINUE_ANYWAY in record.stop_report.options
    assert record.best_snapshot_id is not None


async def test_rollback_restores_best_and_finishes() -> None:
    orchestrator = build((70, 40, 30))
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.STOPPED_EROSION
    # Article head is the regressed iteration-3 text; best snapshot is iter 1.
    record = await orchestrator.apply_decision(record.id, UserDecision(option=StopOption.ROLLBACK))
    assert record.state is RunStateName.DONE
    data = await orchestrator.article_data(record.id)
    intro = next(s for s in data["sections"] if s["id"] == "introduction")
    assert "iteration 1" in intro["text"]


async def test_budget_stop_then_add_budget_revives() -> None:
    agents = ScriptedAgents((50, 55, 85), tokens_per_entry=300_000)
    orchestrator = build(agents=agents)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.STOPPED_BUDGET

    with pytest.raises(RunConflict):
        await orchestrator.apply_decision(record.id, UserDecision(option=StopOption.ADD_BUDGET))

    record = await orchestrator.apply_decision(
        record.id, UserDecision(option=StopOption.ADD_BUDGET, extra_tokens=2_000_000)
    )
    assert record.state is RunStateName.REPLANNING
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.CHECKPOINT  # Q=85 with gates green
    assert record.q_series()[-1] == 85


async def test_needs_user_input_checkpoint_and_resume_with_note() -> None:
    blocking = ScriptedAgents((85,), writer_status=ChangeStatus.NEEDS_USER_INPUT)
    orchestrator = build(agents=blocking)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.CHECKPOINT
    assert record.checkpoint_kind is CheckpointKind.NEEDS_INPUT

    blocking._writer_status = ChangeStatus.OK  # the user supplied the missing input
    record = await orchestrator.apply_decision(
        record.id,
        UserDecision(option=StopOption.CONTINUE, note="Use the attached pilot dataset."),
    )
    assert record.state is RunStateName.REPLANNING
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.CHECKPOINT
    assert record.checkpoint_kind is CheckpointKind.COMPLETION


async def test_quick_edit_skips_planner_but_is_logged() -> None:
    planner = ScriptedPlanner()
    orchestrator = build(planner=planner)
    record = orchestrator.create_run(
        "proj",
        {
            "kind": "quick_edit",
            "section_id": "introduction",
            "instructions": "Rewrite the opening line.",
        },
    )
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.DONE
    assert planner.plan_calls == 0 and planner.replan_calls == []
    assert len(record.entries) == 1
    assert record.entries[0].status is ChangeStatus.OK
    data = await orchestrator.article_data(record.id)
    assert "Scripted text" in data["markdown"]


async def test_user_stop_finalises_idle_run() -> None:
    orchestrator = build()
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.request_stop(record.id)
    assert record.state is RunStateName.STOPPED_USER
    assert record.stop_report is not None
    assert record.stop_report.action is LoopAction.STOP_USER


async def test_planner_outage_stops_on_errors() -> None:
    planner = ScriptedPlanner(plan_failures=99)
    orchestrator = build(planner=planner)
    record = orchestrator.create_run("proj", brief())
    record = await orchestrator.run(record.id)
    assert record.state is RunStateName.STOPPED_ERRORS
    assert planner.plan_calls == Policies().error_streak
    assert record.last_error is not None


async def test_events_are_persisted_in_order() -> None:
    orchestrator = build((85,))
    record = orchestrator.create_run("proj", brief(), on_complete=OnComplete.FINISH)
    record = await orchestrator.run(record.id)
    seqs = [event.seq for event in record.events]
    assert seqs == sorted(seqs)
    assert record.events[0].type == "run_created"
    assert any(event.type == "loop_decision" for event in record.events)
    assert orchestrator.events_after(record.id, after_seq=seqs[0])[0].seq == seqs[1]
