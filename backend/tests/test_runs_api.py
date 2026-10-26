"""Run API surface (Step 9b item 6): auth, intake errors, lifecycle reads,
SSE replay, owner isolation.

No provider bindings are configured in this fixture, on purpose: the API
must behave honestly when accounts are missing - quick edits pause with
``needs_user_input`` (no account is a user condition) and planner-driven
runs stop on errors instead of hanging. The happy path through real agents
is covered at the orchestrator ports in ``test_orchestrator_runner``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.settings import Settings
from tests.orchestrator_support import BRIEF_PAYLOAD

pytestmark = [pytest.mark.api, pytest.mark.orchestrator]


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
def auth(runs_client: TestClient) -> dict[str, str]:
    response = runs_client.post(
        "/api/auth/register",
        json={"email": "runner@example.org", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}


def _create_quick_edit(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/api/projects/proj-1/runs",
        headers=headers,
        json={
            "quick_edit": {
                "section_id": "introduction",
                "instructions": "Tighten the opening paragraph.",
            },
            "wait": True,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()  # type: ignore[no-any-return]


def test_runs_require_auth(runs_client: TestClient) -> None:
    assert runs_client.post("/api/projects/p/runs", json={"brief": {}}).status_code == 401
    assert runs_client.get("/api/runs/01AAAAAAAAAAAAAAAAAAAAAAAAAA").status_code == 401


def test_intake_missing_fields_are_named(runs_client: TestClient, auth: dict[str, str]) -> None:
    payload = dict(BRIEF_PAYLOAD)
    payload.pop("target_words")
    response = runs_client.post("/api/projects/proj-1/runs", headers=auth, json={"brief": payload})
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "needs_user_input"
    assert "target_words" in error["context"]["missing_fields"]


def test_exactly_one_of_brief_or_quick_edit(runs_client: TestClient, auth: dict[str, str]) -> None:
    response = runs_client.post("/api/projects/proj-1/runs", headers=auth, json={})
    assert response.status_code == 422


def test_quick_edit_without_account_pauses_for_the_user(
    runs_client: TestClient, auth: dict[str, str]
) -> None:
    run = _create_quick_edit(runs_client, auth)
    assert run["kind"] == "quick_edit"
    assert run["state"] == "CHECKPOINT"
    assert run["checkpoint_kind"] == "needs_input"

    fetched = runs_client.get(f"/api/runs/{run['id']}", headers=auth)
    assert fetched.status_code == 200
    ledger = runs_client.get(f"/api/runs/{run['id']}/ledger", headers=auth).json()
    assert ledger["entries"][0]["status"] == "needs_user_input"
    usage = runs_client.get(f"/api/runs/{run['id']}/usage", headers=auth).json()
    assert usage["total"] == 0
    digest = runs_client.get(f"/api/runs/{run['id']}/digest", headers=auth).json()
    assert digest["tokens"] <= digest["max_tokens"]
    article = runs_client.get(f"/api/runs/{run['id']}/article", headers=auth).json()
    assert article["run_id"] == run["id"]


def test_events_sse_replay(runs_client: TestClient, auth: dict[str, str]) -> None:
    run = _create_quick_edit(runs_client, auth)
    response = runs_client.get(
        f"/api/runs/{run['id']}/events", headers=auth, params={"once": "true"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: run_created" in response.text
    assert "event: checkpoint_reached" in response.text


def test_stop_and_decision_flow(runs_client: TestClient, auth: dict[str, str]) -> None:
    run = _create_quick_edit(runs_client, auth)
    stopped = runs_client.post(f"/api/runs/{run['id']}/stop", headers=auth)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "STOPPED_USER"

    bad = runs_client.post(
        f"/api/runs/{run['id']}/decision", headers=auth, json={"option": "add_budget"}
    )
    assert bad.status_code == 409  # add_budget without an amount

    decided = runs_client.post(
        f"/api/runs/{run['id']}/decision",
        headers=auth,
        json={"option": "continue_anyway", "extra_tokens": 10000},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] in {"EXECUTING", "REPLANNING"}


def test_brief_run_without_bindings_stops_on_errors(
    runs_client: TestClient, auth: dict[str, str]
) -> None:
    response = runs_client.post(
        "/api/projects/proj-1/runs",
        headers=auth,
        json={"brief": BRIEF_PAYLOAD, "wait": True},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["state"] == "STOPPED_ERRORS"
    assert run["stop_report"]["action"] == "STOP_ERRORS"
    article = runs_client.get(f"/api/runs/{run['id']}/article", headers=auth).json()
    assert article["sections"]  # rubric sections were seeded before planning


def test_other_owners_run_is_404(runs_client: TestClient, auth: dict[str, str]) -> None:
    run = _create_quick_edit(runs_client, auth)
    second = runs_client.post(
        "/api/auth/register",
        json={"email": "other@example.org", "password": "correct-horse-battery"},
    ).json()
    other = {"authorization": f"Bearer {second['access_token']}"}
    assert runs_client.get(f"/api/runs/{run['id']}", headers=other).status_code == 404
    assert runs_client.post(f"/api/runs/{run['id']}/stop", headers=other).status_code == 404
