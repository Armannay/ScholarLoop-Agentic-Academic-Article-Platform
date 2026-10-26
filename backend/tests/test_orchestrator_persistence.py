"""Persistence & state machine: transition table, file store round-trip,
and crash-resume with task-level idempotency (FileRunStore on disk)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.policies import Policies
from app.orchestrator.errors import InvalidTransition, RunNotFound
from app.orchestrator.persistence import FileRunStore, InMemoryRunStore
from app.orchestrator.runner import Orchestrator, OrchestratorConfig
from app.orchestrator.state_machine import (
    RunStateName,
    StateMachine,
    can_transition,
)
from tests.orchestrator_support import CrashingAgents, ScriptedAgents, ScriptedPlanner, brief

pytestmark = pytest.mark.orchestrator

FAST = OrchestratorConfig(backoff_base_s=0.0, planner_backoff_s=0.0)


def test_transition_table() -> None:
    assert can_transition(RunStateName.INTAKE, RunStateName.PLANNING)
    assert can_transition(RunStateName.INTAKE, RunStateName.EXECUTING)  # quick edit
    assert can_transition(RunStateName.DECIDING, RunStateName.CHECKPOINT)
    assert can_transition(RunStateName.STOPPED_BUDGET, RunStateName.REPLANNING)
    assert not can_transition(RunStateName.DONE, RunStateName.REPLANNING)
    assert not can_transition(RunStateName.INTAKE, RunStateName.DONE)
    assert not can_transition(RunStateName.VALIDATING, RunStateName.EXECUTING)


def test_invalid_transition_is_refused_and_unsaved() -> None:
    store = InMemoryRunStore()
    machine = StateMachine(store)
    orchestrator = Orchestrator(ScriptedPlanner(), ScriptedAgents(), store=store, config=FAST)
    record = orchestrator.create_run("proj", brief())
    before = store.load(record.id)
    with pytest.raises(InvalidTransition):
        machine.transition(record, RunStateName.DONE)
    assert store.load(record.id).state is before.state


def test_file_store_round_trip_and_missing(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "runs")
    orchestrator = Orchestrator(ScriptedPlanner(), ScriptedAgents(), store=store, config=FAST)
    record = orchestrator.create_run("proj", brief())
    loaded = FileRunStore(tmp_path / "runs").load(record.id)
    assert loaded.id == record.id
    assert loaded.brief == record.brief
    assert store.list_ids() == [record.id]
    with pytest.raises(RunNotFound):
        store.load("01AAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert store.delete(record.id) is True
    assert store.get(record.id) is None


async def test_crash_resume_never_reruns_completed_tasks(tmp_path: Path) -> None:
    """Kill the run mid-EXECUTING; a fresh orchestrator over the same files
    resumes, and the already-applied writer task is not executed again."""
    store = FileRunStore(tmp_path / "runs")
    agents = ScriptedAgents((85,))
    crashing = CrashingAgents(agents)
    first = Orchestrator(ScriptedPlanner(), crashing, policies=Policies(), store=store, config=FAST)
    record = first.create_run("proj", brief())
    with pytest.raises(CrashingAgents.Crash):  # type: ignore[attr-defined]
        await first.run(record.id)

    # The writer's entry and patch were persisted before the crash.
    crashed = store.load(record.id)
    assert crashed.state in {RunStateName.EXECUTING, RunStateName.VALIDATING}
    writer_entries = [entry for entry in crashed.entries if entry.role.value == "writer"]
    assert len(writer_entries) == 1
    writer_task_id = writer_entries[0].task_id
    intro = next(s for s in crashed.article.sections if s.state.id == "introduction")
    assert "Scripted text" in intro.text

    # Resume with a brand-new orchestrator (empty runtime cache) on the files.
    healthy = ScriptedAgents((85,))
    second = Orchestrator(ScriptedPlanner(), healthy, policies=Policies(), store=store, config=FAST)
    resumed = await second.run(record.id)
    assert writer_task_id not in healthy.executed
    assert resumed.state is RunStateName.CHECKPOINT  # Q=85, gates green, ask
    assert resumed.q_series() == [85]
    data = await second.article_data(record.id)
    assert "Scripted text" in data["markdown"]
