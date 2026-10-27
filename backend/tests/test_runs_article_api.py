"""Run API read models and conflicts (Step 9b item 6, against the frozen spec).

Covered here: ``events/history`` paging, ``quality``, section ``history`` and
``diff``, ``snapshots``, ``restore`` (dry-run and for real), the
``active_run`` guard (one project, one live run) and the ``reason`` contexts
on ``run_conflict`` - all checked against the shapes in
``docs/openapi/openapi.yaml`` and ``docs/openapi/examples/``.

Runs with real article content are seeded by driving a scripted Orchestrator
over the *same* FileRunStore directory the app uses, bypassing the provider
wiring (no accounts configured in these fixtures, so the API path could never
produce content - HARD RULE 5 keeps it all offline and deterministic).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.rubric import Rubric
from app.core.settings import Settings
from app.orchestrator.errors import RunConflict
from app.orchestrator.persistence import FileRunStore
from app.orchestrator.runner import Orchestrator, OrchestratorConfig
from app.orchestrator.state_machine import RunStateName
from tests.orchestrator_support import BRIEF_PAYLOAD, ScriptedAgents, ScriptedPlanner

pytestmark = [pytest.mark.api, pytest.mark.orchestrator]

FAST = OrchestratorConfig(backoff_base_s=0.0, planner_backoff_s=0.0)
PROJECT = "peer-review-2026"


@pytest.fixture
def runs_client(
    repo_root: Path, db_url: str, tmp_path: Path, app_master_key_env: str
) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        root_dir=repo_root,
        env="test",
        database_url=db_url,
        secret_key=None,
        runs_dir=tmp_path / "runs",
    )
    with TestClient(create_app(settings)) as client:
        yield client  # type: ignore[misc]


@pytest.fixture
def owner(runs_client: TestClient) -> dict[str, str]:
    response = runs_client.post(
        "/api/auth/register",
        json={"email": "reader@example.org", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "headers": {"authorization": f"Bearer {body['access_token']}"},
        "id": body["user"]["id"],
    }  # type: ignore[return-value]


def _seed_checkpoint_run(
    runs_dir: Path,
    owner_id: str,
    rubric: Rubric,
    *,
    project: str = PROJECT,
    qs: tuple[int, ...] = (85,),
) -> str:
    """Drive a scripted run to its completion checkpoint on the app's store.

    The rubric is passed so the full section set (abstract + rubric sections)
    is seeded exactly like the API-wired orchestrator does.
    """

    orchestrator = Orchestrator(
        ScriptedPlanner(),
        ScriptedAgents(qs),
        rubric=rubric,
        store=FileRunStore(runs_dir),
        config=FAST,
    )

    async def drive() -> str:
        record = orchestrator.create_run(project, dict(BRIEF_PAYLOAD), owner_id=owner_id)
        record = await orchestrator.run(record.id)
        assert record.state is RunStateName.CHECKPOINT, record.model_dump_json()
        return record.id

    return asyncio.run(drive())


@pytest.fixture
def seeded(runs_client: TestClient, owner: dict[str, str], tmp_path: Path, rubric: Rubric) -> str:
    """A checkpointed run with two iterations of content (qs 50 -> 85)."""
    return _seed_checkpoint_run(tmp_path / "runs", owner["id"], rubric, qs=(50, 85))


def _events_of_type(events: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    return [event for event in events if event["type"] == event_type]


# --------------------------------------------------------------------------- events/history
def test_events_history_paging(runs_client: TestClient, owner: dict[str, str], seeded: str) -> None:
    headers = owner["headers"]
    first_page = runs_client.get(
        f"/api/runs/{seeded}/events/history", headers=headers, params={"limit": 3}
    )
    assert first_page.status_code == 200, first_page.text
    page = first_page.json()
    assert page["run_id"] == seeded
    assert [event["seq"] for event in page["events"]] == [0, 1, 2]
    assert page["events"][0]["type"] == "run_created"
    assert page["next_after"] == 2

    rest = runs_client.get(
        f"/api/runs/{seeded}/events/history",
        headers=headers,
        params={"after": page["next_after"], "limit": 1000},
    ).json()
    assert rest["events"][0]["seq"] == 3
    assert rest["next_after"] is None  # log exhausted
    assert _events_of_type(rest["events"], "checkpoint_reached")

    entire = runs_client.get(f"/api/runs/{seeded}/events/history", headers=headers).json()
    assert entire["events"][0]["seq"] == 0
    assert len(entire["events"]) == 3 + len(rest["events"])


def test_events_history_owner_isolation(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    second = runs_client.post(
        "/api/auth/register",
        json={"email": "stranger@example.org", "password": "correct-horse-battery"},
    ).json()
    other = {"authorization": f"Bearer {second['access_token']}"}
    for suffix in ("events/history", "quality", "article/snapshots"):
        response = runs_client.get(f"/api/runs/{seeded}/{suffix}", headers=other)
        assert response.status_code == 404, suffix
    restore = runs_client.post(
        f"/api/runs/{seeded}/article/restore", headers=other, json={"snapshot_id": "x"}
    )
    assert restore.status_code == 404


# --------------------------------------------------------------------------- quality
def test_quality_view(runs_client: TestClient, owner: dict[str, str], seeded: str) -> None:
    response = runs_client.get(f"/api/runs/{seeded}/quality", headers=owner["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == seeded
    assert body["latest"]["Q"] == 85
    assert all(body["latest"]["gates"].values())
    assert [point["q"] for point in body["series"]] == [50, 85]
    assert [point["iter"] for point in body["series"]] == [1, 2]
    assert body["series"][-1]["gates_passed"] is True
    assert body["best_q"] == 85.0


def test_quality_view_before_any_validation(runs_client: TestClient, owner: dict[str, str]) -> None:
    # A quick edit never validates, so quality stays empty (latest is null).
    created = runs_client.post(
        "/api/projects/idle-proj/runs",
        headers=owner["headers"],
        json={"quick_edit": {"section_id": "introduction", "instructions": "noop"}, "wait": True},
    )
    assert created.status_code == 202, created.text
    body = runs_client.get(
        f"/api/runs/{created.json()['id']}/quality", headers=owner["headers"]
    ).json()
    assert body["latest"] is None
    assert body["series"] == []
    assert body["best_q"] is None


# --------------------------------------------------------------------------- section history
def test_section_history_matches_ledger(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    history = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/history", headers=headers
    )
    assert history.status_code == 200, history.text
    versions = history.json()["versions"]
    assert [version["version"] for version in versions] == [1, 2]  # oldest first
    assert all(version["section_id"] == "introduction" for version in versions)
    assert versions[0]["chars_added"] > 0 and versions[0]["chars_removed"] == 0

    ledger = runs_client.get(f"/api/runs/{seeded}/ledger", headers=headers).json()["entries"]
    writer_entries = [entry for entry in ledger if entry["role"] == "writer"]
    # The version row points at the ledger entry that produced it (audit trail).
    assert {version["entry_id"] for version in versions} == {
        entry["id"] for entry in writer_entries
    }
    assert all(version["patch_id"] for version in versions)
    assert versions[-1]["q_after"] == 85.0

    unknown = runs_client.get(
        f"/api/runs/{seeded}/article/sections/nowhere/history", headers=headers
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- section diff
def test_section_diff_defaults_and_params(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    response = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from"]["version"] == 1  # previous version of head (=2)
    assert body["to"]["version"] == 2
    assert body["diff"]["stats"]["chars_added"] > 0
    assert body["diff"]["lines_added"] == 1 and body["diff"]["lines_removed"] == 1
    assert "introduction@1" in body["diff"]["unified"]
    assert "-Scripted text, iteration 1." in body["diff"]["unified"]
    assert "+Scripted text, iteration 2." in body["diff"]["unified"]

    first_text = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff",
        headers=headers,
        params={"from": 0, "to": 1},
    ).json()
    assert first_text["to"]["version"] == 1
    assert first_text["diff"]["lines_removed"] == 0  # empty baseline: pure additions
    assert first_text["from"]["hash"].startswith("sha256:")

    same = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff",
        headers=headers,
        params={"from": 1, "to": 1},
    ).json()
    assert same["diff"]["stats"] == {
        "chars_added": 0,
        "chars_removed": 0,
        "net_chars": 0,
        "touched_chars": 0,
    }
    assert same["diff"]["lines_added"] == 0


def test_section_diff_range_errors(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    inverted = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff",
        headers=headers,
        params={"from": 2, "to": 1},
    )
    assert inverted.status_code == 409
    error = inverted.json()["error"]
    assert error["code"] == "run_conflict"
    assert error["context"]["reason"] == "invalid_version_range"

    beyond = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff",
        headers=headers,
        params={"to": 99},
    )
    assert beyond.status_code == 404

    unknown = runs_client.get(f"/api/runs/{seeded}/article/sections/nowhere/diff", headers=headers)
    assert unknown.status_code == 404

    negative = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/diff",
        headers=headers,
        params={"from": -1},
    )
    assert negative.status_code == 422  # Query(ge=0)


# --------------------------------------------------------------------------- snapshots
def test_snapshots_list_newest_first(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    run = runs_client.get(f"/api/runs/{seeded}", headers=headers).json()
    response = runs_client.get(f"/api/runs/{seeded}/article/snapshots", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["best_snapshot_id"] == run["best_snapshot_id"]
    labels = [snapshot["label"] for snapshot in body["snapshots"]]
    assert labels == ["best-iter-2", "best-iter-1"]  # newest first
    best = body["snapshots"][0]
    assert best["id"] == body["best_snapshot_id"]
    assert best["q"] == 85.0 and best["iter"] == 2
    assert "introduction" in best["section_ids"]
    assert best["total_words"] > 0


# --------------------------------------------------------------------------- restore
def test_restore_dry_run_then_real(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    snapshots = runs_client.get(f"/api/runs/{seeded}/article/snapshots", headers=headers).json()
    older = next(snapshot for snapshot in snapshots["snapshots"] if snapshot["iter"] == 1)

    dry = runs_client.post(
        f"/api/runs/{seeded}/article/restore",
        headers=headers,
        json={"snapshot_id": older["id"], "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    preview = dry.json()
    assert preview["run_id"] == seeded
    assert preview["snapshot_id"] == older["id"]
    assert preview["label"] == older["label"]
    assert preview["q"] == 50.0
    assert preview["restored"] == ["introduction"]
    assert "abstract" in preview["unchanged"]
    assert preview["versions"] == {"introduction": 3}
    assert preview["state"] == "CHECKPOINT"
    assert preview["dry_run"] is True
    assert preview["updated_at"]
    # A dry run writes nothing: still two versions, no rollback event.
    versions = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/history", headers=headers
    ).json()["versions"]
    assert len(versions) == 2
    events = runs_client.get(f"/api/runs/{seeded}/events/history", headers=headers).json()["events"]
    assert not _events_of_type(events, "rollback_applied")

    real = runs_client.post(
        f"/api/runs/{seeded}/article/restore",
        headers=headers,
        json={"snapshot_id": older["id"]},
    )
    assert real.status_code == 200, real.text
    result = real.json()
    assert result["dry_run"] is False
    assert result["restored"] == ["introduction"]
    assert result["versions"]["introduction"] == 3
    assert result["state"] == "CHECKPOINT"  # restore never moves the loop
    assert "abstract" in result["unchanged"]

    versions = runs_client.get(
        f"/api/runs/{seeded}/article/sections/introduction/history", headers=headers
    ).json()["versions"]
    assert [version["version"] for version in versions] == [1, 2, 3]
    article = runs_client.get(f"/api/runs/{seeded}/article", headers=headers).json()
    intro = next(s for s in article["sections"] if s["id"] == "introduction")
    assert "Scripted text, iteration 1." in intro["text"]
    events = runs_client.get(f"/api/runs/{seeded}/events/history", headers=headers).json()["events"]
    assert _events_of_type(events, "rollback_applied")


def test_restore_unknown_snapshot_is_404(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    response = runs_client.post(
        f"/api/runs/{seeded}/article/restore",
        headers=owner["headers"],
        json={"snapshot_id": "01AAAAAAAAAAAAAAAAAAAAAAAAAA"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_restore_refused_while_executing(
    runs_client: TestClient, owner: dict[str, str], seeded: str, tmp_path: Path
) -> None:
    store = FileRunStore(tmp_path / "runs")
    record = store.load(seeded)
    record.state = RunStateName.EXECUTING  # crashed mid-wave, record says executing
    store.save(record)
    snapshot_id = runs_client.get(
        f"/api/runs/{seeded}/article/snapshots", headers=owner["headers"]
    ).json()["best_snapshot_id"]
    response = runs_client.post(
        f"/api/runs/{seeded}/article/restore",
        headers=owner["headers"],
        json={"snapshot_id": snapshot_id},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "run_conflict"
    assert error["context"]["reason"] == "restore_while_running"


# --------------------------------------------------------------------------- active_run guard
def test_active_run_guard_blocks_second_run(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    duplicate = runs_client.post(
        f"/api/projects/{PROJECT}/runs", headers=headers, json={"brief": BRIEF_PAYLOAD}
    )
    assert duplicate.status_code == 409, duplicate.text
    error = duplicate.json()["error"]
    assert error["code"] == "run_conflict"
    assert error["context"]["reason"] == "active_run"
    assert error["context"]["active_run_id"] == seeded
    assert error["context"]["state"] == "CHECKPOINT"
    assert error["context"]["project_id"] == PROJECT

    # A stopped run is not live: create is accepted again after the stop.
    stopped = runs_client.post(f"/api/runs/{seeded}/stop", headers=headers)
    assert stopped.status_code == 200
    response = runs_client.post(
        f"/api/projects/{PROJECT}/runs",
        headers=headers,
        json={"quick_edit": {"section_id": "introduction", "instructions": "noop"}, "wait": True},
    )
    assert response.status_code == 202, response.text


def test_active_run_guard_is_owner_scoped(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    another = runs_client.post(
        "/api/auth/register",
        json={"email": "second@example.org", "password": "correct-horse-battery"},
    ).json()
    other = {"authorization": f"Bearer {another['access_token']}"}
    response = runs_client.post(
        f"/api/projects/{PROJECT}/runs",
        headers=other,
        json={"quick_edit": {"section_id": "introduction", "instructions": "noop"}, "wait": True},
    )
    assert response.status_code == 202, response.text  # the guard never leaks across owners


def test_intake_still_beats_the_guard(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    bad_brief = dict(BRIEF_PAYLOAD)
    bad_brief.pop("topic")
    response = runs_client.post(
        f"/api/projects/{PROJECT}/runs", headers=owner["headers"], json={"brief": bad_brief}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "needs_user_input"
    assert "topic" in response.json()["error"]["context"]["missing_fields"]


def test_project_id_pattern_enforced(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    response = runs_client.post(
        "/api/projects/Bad Project/runs",
        headers=owner["headers"],
        json={"quick_edit": {"section_id": "introduction", "instructions": "noop"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- decision conflicts
def test_decision_on_done_run_names_invalid_transition(
    runs_client: TestClient, owner: dict[str, str], seeded: str
) -> None:
    headers = owner["headers"]
    finished = runs_client.post(
        f"/api/runs/{seeded}/decision", headers=headers, json={"option": "finish"}
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["state"] == "DONE"

    again = runs_client.post(
        f"/api/runs/{seeded}/decision", headers=headers, json={"option": "continue"}
    )
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "run_conflict"
    assert error["context"]["reason"] == "invalid_transition"
    assert error["context"]["state"] == "DONE"


# ------------------------------------------------------------ guard semantics (unit level)
def test_guard_sees_only_live_states() -> None:
    orchestrator = Orchestrator(ScriptedPlanner(), ScriptedAgents(), config=FAST)
    first = orchestrator.create_run("proj-alpha", dict(BRIEF_PAYLOAD))
    with pytest.raises(RunConflict) as raised:
        orchestrator.create_run("proj-alpha", dict(BRIEF_PAYLOAD))
    assert raised.value.context["reason"] == "active_run"
    # A different project (or a stopped run) is unaffected.
    orchestrator.create_run("proj-beta", dict(BRIEF_PAYLOAD))

    async def stop_and_retry() -> None:
        await orchestrator.request_stop(first.id)
        orchestrator.create_run("proj-alpha", dict(BRIEF_PAYLOAD))

    asyncio.run(stop_and_retry())
