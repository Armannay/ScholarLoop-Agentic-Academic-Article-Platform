"""``GET /api/projects/{project_id}/runs`` - one project's run history.

The frozen spec (``docs/openapi/openapi.yaml``, ``listProjectRuns``) fixes four things
this module checks:

* the payload is ``RunList`` - ``{runs: RunSummary[], total}`` with no brief, no plan
  and no article bodies (the field set is compared to
  ``docs/openapi/examples/runs/list-project-runs.response.200.json`` verbatim);
* the order is **newest first**, and ``total`` counts the project, not the page;
* ``limit`` defaults to 50 and is bounded to ``1..200``;
* the list is owner-scoped: another owner's project answers ``404 not_found`` rather
  than an empty page that would confirm the label exists.

Runs are seeded straight into the app's ``FileRunStore`` directory (a persisted
``RunRecord`` per run). Listing is a pure read, so it needs no provider, no account
and no orchestrator: exactly the property the endpoint has to have.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.contracts.brief import Brief
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.core.ids import new_ulid
from app.core.rubric import Rubric
from app.core.settings import Settings
from app.orchestrator.intake import IntakeKind
from app.orchestrator.persistence import FileRunStore, RunRecord
from app.orchestrator.runner import Orchestrator, OrchestratorConfig
from app.orchestrator.state_machine import CheckpointKind, RunStateName
from tests.orchestrator_support import BRIEF_PAYLOAD, ScriptedAgents, ScriptedPlanner

pytestmark = [pytest.mark.api, pytest.mark.orchestrator]

FAST = OrchestratorConfig(backoff_base_s=0.0, planner_backoff_s=0.0)

PROJECT = "peer-review-2026"
#: The field set of one list row, straight from the frozen example document.
EXAMPLE_ROW_FIELDS = {
    "id",
    "project_id",
    "kind",
    "state",
    "iter",
    "best_q",
    "checkpoint_kind",
    "running",
    "tokens_spent",
    "created_at",
    "updated_at",
}


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def runs_client(
    repo_root: Path, db_url: str, runs_dir: Path, app_master_key_env: str
) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        root_dir=repo_root,
        env="test",
        database_url=db_url,
        secret_key=None,
        runs_dir=runs_dir,
    )
    with TestClient(create_app(settings)) as client:
        yield client  # type: ignore[misc]


def _register(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 201, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def owner(runs_client: TestClient) -> dict[str, str]:
    return _register(runs_client, "lister@example.org")


@pytest.fixture
def other_owner(runs_client: TestClient) -> dict[str, str]:
    return _register(runs_client, "someone-else@example.org")


def _owner_id(runs_client: TestClient, headers: dict[str, str]) -> str:
    response = runs_client.get("/api/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _seed(
    runs_dir: Path,
    owner_id: str,
    *,
    project_id: str = PROJECT,
    created_at: datetime,
    state: RunStateName = RunStateName.DONE,
    kind: IntakeKind = IntakeKind.BRIEF,
    best_q: float | None = None,
    checkpoint_kind: CheckpointKind | None = None,
    tokens_out: int = 0,
) -> str:
    """Persist one run record the way the orchestrator would (no provider involved)."""
    entry = ChangeEntry(
        id=new_ulid(),
        iter=1,
        task_id=new_ulid(),
        role="writer",
        targets=["introduction"],
        summary="seeded row",
        tokens=TokenUsage(in_=0, out=tokens_out, cached=0),
        status="ok",
    )
    record = RunRecord(
        id=new_ulid(),
        project_id=project_id,
        owner_id=owner_id,
        kind=kind,
        state=state,
        brief=Brief.model_validate(BRIEF_PAYLOAD),
        iter=1,
        entries=[entry],
        best_q=best_q,
        checkpoint_kind=checkpoint_kind,
        created_at=created_at,
        updated_at=created_at,
    )
    FileRunStore(runs_dir).save(record)
    return record.id


def _create_project(runs_client: TestClient, headers: dict[str, str], *, name: str) -> str:
    response = runs_client.post(
        "/api/projects",
        headers=headers,
        json={"name": name, "brief": dict(BRIEF_PAYLOAD)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --------------------------------------------------------------------------- auth & 404
def test_list_requires_auth(runs_client: TestClient) -> None:
    assert runs_client.get(f"/api/projects/{PROJECT}/runs").status_code == 401


def test_unknown_label_is_404(runs_client: TestClient, owner: dict[str, str]) -> None:
    response = runs_client.get("/api/projects/never-used/runs", headers=owner)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["context"]["project_id"] == "never-used"


def test_project_id_pattern_is_enforced(runs_client: TestClient, owner: dict[str, str]) -> None:
    """The spec's ``ProjectId`` is ``[a-z0-9][a-z0-9-]{2,63}``: two characters is not."""
    assert runs_client.get("/api/projects/ab/runs", headers=owner).status_code == 422
    assert runs_client.get("/api/projects/Peer-Review/runs", headers=owner).status_code == 422


def test_another_owners_project_reads_as_404(
    runs_client: TestClient,
    owner: dict[str, str],
    other_owner: dict[str, str],
    runs_dir: Path,
) -> None:
    stranger = _owner_id(runs_client, other_owner)
    _seed(runs_dir, stranger, created_at=datetime(2026, 1, 1, tzinfo=UTC))

    response = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"

    # ... and the rows never leak into another label's list either.
    mine = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=other_owner)
    assert mine.status_code == 200
    assert mine.json()["total"] == 1


# --------------------------------------------------------------------------- payload
def test_fresh_project_lists_empty(runs_client: TestClient, owner: dict[str, str]) -> None:
    """A stored project with no runs exists, so it answers an empty page - not 404."""
    project_id = _create_project(runs_client, owner, name="Peer review 2026")

    response = runs_client.get(f"/api/projects/{project_id}/runs", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json() == {"runs": [], "total": 0}


def test_legacy_label_with_runs_stays_listable(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path
) -> None:
    """Pre-Step-11 labels have runs but no project row; they must not disappear."""
    _seed(
        runs_dir,
        _owner_id(runs_client, owner),
        project_id="legacy" + "-label",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    response = runs_client.get("/api/projects/legacy-label/runs", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_rows_carry_exactly_the_spec_fields(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path, repo_root: Path
) -> None:
    _seed(
        runs_dir,
        _owner_id(runs_client, owner),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        state=RunStateName.CHECKPOINT,
        best_q=78.0,
        checkpoint_kind=CheckpointKind.COMPLETION,
        tokens_out=4180,
    )

    body = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner).json()
    assert set(body) == {"runs", "total"}
    row = body["runs"][0]
    assert set(row) == EXAMPLE_ROW_FIELDS
    assert row["best_q"] == 78.0
    assert row["checkpoint_kind"] == "completion"
    assert row["tokens_spent"] == 4180
    assert row["running"] is False
    assert row["kind"] == "brief"
    assert row["state"] == "CHECKPOINT"


def test_field_set_matches_the_frozen_example(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path, repo_root: Path
) -> None:
    """The example document is the wire contract; no field may drift from it."""
    example = json.loads(
        (repo_root / "docs/openapi/examples/runs/list-project-runs.response.200.json").read_text(
            encoding="utf-8"
        )
    )["value"]
    _seed(
        runs_dir,
        _owner_id(runs_client, owner),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        best_q=78.0,
        checkpoint_kind=CheckpointKind.COMPLETION,
    )

    body = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner).json()
    assert set(body) == set(example)
    assert set(body["runs"][0]) == set(example["runs"][0])


# --------------------------------------------------------------------------- order & paging
def test_newest_first_with_total_beyond_the_page(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path
) -> None:
    owner_id = _owner_id(runs_client, owner)
    base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    oldest = _seed(runs_dir, owner_id, created_at=base, state=RunStateName.DONE)
    middle = _seed(
        runs_dir, owner_id, created_at=base + timedelta(hours=1), state=RunStateName.REPLANNING
    )
    newest = _seed(
        runs_dir,
        owner_id,
        created_at=base + timedelta(hours=2),
        state=RunStateName.CHECKPOINT,
        kind=IntakeKind.QUICK_EDIT,
        checkpoint_kind=CheckpointKind.NEEDS_INPUT,
    )
    # Noise: another project, and the same label under another owner.
    _seed(runs_dir, owner_id, project_id="another-project", created_at=base + timedelta(hours=3))

    page = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner, params={"limit": 2})
    assert page.status_code == 200, page.text
    body = page.json()
    assert [row["id"] for row in body["runs"]] == [newest, middle]
    assert body["total"] == 3
    assert body["runs"][0]["kind"] == "quick_edit"
    assert body["runs"][0]["checkpoint_kind"] == "needs_input"

    whole = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner).json()
    assert [row["id"] for row in whole["runs"]] == [newest, middle, oldest]
    assert whole["total"] == 3


def test_default_limit_is_fifty(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path
) -> None:
    owner_id = _owner_id(runs_client, owner)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(55):
        _seed(runs_dir, owner_id, created_at=base + timedelta(minutes=index))

    body = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner).json()
    assert len(body["runs"]) == 50
    assert body["total"] == 55
    # Newest first: the last seeded record is the first row.
    assert body["runs"][0]["created_at"].startswith("2026-01-01T00:54")


def test_limit_is_bounded_to_the_spec_range(runs_client: TestClient, owner: dict[str, str]) -> None:
    assert (
        runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner, params={"limit": 0})
    ).status_code == 422
    assert (
        runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner, params={"limit": 201})
    ).status_code == 422
    assert (
        runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner, params={"limit": "many"})
    ).status_code == 422


# --------------------------------------------------------------------------- store edge cases
def test_an_unreadable_record_does_not_blind_the_list(
    runs_client: TestClient, owner: dict[str, str], runs_dir: Path
) -> None:
    """A half-written file from a killed process is skipped with a warning, not a 500."""
    _seed(runs_dir, _owner_id(runs_client, owner), created_at=datetime(2026, 1, 1, tzinfo=UTC))
    (runs_dir / f"{new_ulid()}.json").write_text("{not json", encoding="utf-8")

    response = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_project_runs_read_model_without_an_owner(runs_dir: Path, rubric: Rubric) -> None:
    """``owner_id=None`` is the internal "every owner" call the API never makes."""
    owner_id = new_ulid()
    other_id = new_ulid()
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    _seed(runs_dir, owner_id, created_at=earlier)
    _seed(runs_dir, other_id, created_at=earlier + timedelta(minutes=1))

    orchestrator = Orchestrator(
        ScriptedPlanner(),
        ScriptedAgents((80,)),
        rubric=rubric,
        store=FileRunStore(runs_dir),
        config=FAST,
    )

    everyone, total = orchestrator.project_runs(PROJECT)
    assert total == 2
    assert len(everyone) == 2
    # Newest first, regardless of owner.
    assert everyone[0].owner_id == other_id

    mine, mine_total = orchestrator.project_runs(PROJECT, owner_id=owner_id)
    assert (len(mine), mine_total) == (1, 1)
    assert mine[0].owner_id == owner_id

    page, page_total = orchestrator.project_runs(PROJECT, limit=0)
    assert (page, page_total) == ([], 2)


def test_a_real_run_shows_its_state_and_cost(
    runs_client: TestClient,
    owner: dict[str, str],
    runs_dir: Path,
    rubric: Rubric,
) -> None:
    """Drive a scripted run to its checkpoint; the row mirrors the record, no bodies."""
    orchestrator = Orchestrator(
        ScriptedPlanner(),
        ScriptedAgents((50, 85)),
        rubric=rubric,
        store=FileRunStore(runs_dir),
        config=FAST,
    )

    async def drive() -> RunRecord:
        record = orchestrator.create_run(
            PROJECT, dict(BRIEF_PAYLOAD), owner_id=_owner_id(runs_client, owner)
        )
        return await orchestrator.run(record.id)

    record = asyncio.run(drive())

    body = runs_client.get(f"/api/projects/{PROJECT}/runs", headers=owner).json()
    assert body["total"] == 1
    row = body["runs"][0]
    assert row["id"] == record.id
    assert row["state"] == record.state.value == "CHECKPOINT"
    assert row["iter"] == record.iter
    assert row["best_q"] == record.best_q
    assert row["tokens_spent"] == record.total_tokens_spent()
    assert not {"brief", "plan", "decisions", "sections"} & set(row)
