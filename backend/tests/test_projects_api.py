"""Projects API: CRUD, ownership, keyset pagination, validation and the 409 guard.

The suite pins both the allowed and the denied paths:

* every mutation needs a bearer token;
* another user's project reads as **404**, never 403 (the API must not confirm that a
  foreign id exists);
* an invalid ``Brief`` is a 422 with *per-field* errors and creates nothing;
* paging a list with ``limit``/``cursor`` visits every project exactly once;
* PATCH/DELETE answer 409 ``conflict`` with ``context.reason=active_run`` while the
  project has a live run, and work again once the run settles at a checkpoint;
* no brief text ever reaches a log line or the audit trail.

The runs seam is the in-memory fake (:class:`app.projects.runs.InMemoryRunsQuery`), so
these tests never boot an orchestrator, provider pool or account store. One test
checks the real wiring resolves to ``StoreRunsQuery`` over the app's run store.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts.models import AuditLogRow
from app.api.main import create_app
from app.core.settings import Settings
from app.projects.runs import InMemoryRunsQuery, RunFacts, StoreRunsQuery
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.api

PASS = "correct-horse-battery"
BRIEF = dict(SAMPLE_BRIEF)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def projects_client(
    repo_root: Path, db_url: str, tmp_path: Path, app_master_key_env: str
) -> Iterator[TestClient]:
    """App on a throwaway database, with the runs seam pinned to an in-memory fake."""
    settings = Settings(
        _env_file=None,
        root_dir=repo_root,
        env="test",
        database_url=db_url,
        secret_key=None,
        runs_dir=tmp_path / "runs",
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.runs_query = InMemoryRunsQuery()  # type: ignore[attr-defined]
        yield client  # type: ignore[misc]


@pytest.fixture
def default_client(
    repo_root: Path, db_url: str, tmp_path: Path, app_master_key_env: str
) -> Iterator[TestClient]:
    """Same app *without* a runs override: exercises the real store wiring."""
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
def runs(projects_client: TestClient) -> InMemoryRunsQuery:
    """The fake runs seam installed on the app."""
    return projects_client.app.state.runs_query  # type: ignore[no-any-return]


def register(client: TestClient, email: str = "owner@example.org") -> dict[str, str]:
    response = client.post("/api/auth/register", json={"email": email, "password": PASS})
    assert response.status_code == 201, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def headers(projects_client: TestClient) -> dict[str, str]:
    return register(projects_client)


def create_project(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Peer review 2026",
    brief: dict | None = None,
    **extra: object,
) -> dict:
    payload: dict = {"name": name, "brief": dict(brief if brief is not None else BRIEF), **extra}
    response = client.post("/api/projects", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- auth
def test_every_project_route_requires_auth(projects_client: TestClient) -> None:
    pid = sample_ulid(0)
    assert projects_client.get("/api/projects").status_code == 401
    assert (
        projects_client.post("/api/projects", json={"name": "p", "brief": BRIEF}).status_code == 401
    )
    assert projects_client.get(f"/api/projects/{pid}").status_code == 401
    assert projects_client.patch(f"/api/projects/{pid}", json={"name": "x"}).status_code == 401
    assert projects_client.delete(f"/api/projects/{pid}").status_code == 401


# --------------------------------------------------------------------------- CRUD
def test_create_and_read_round_trip(projects_client: TestClient, headers: dict[str, str]) -> None:
    created = create_project(
        projects_client,
        headers,
        description="A study of review checklists.",
        policy_overrides={"q_min": 80},
    )

    assert created["name"] == "Peer review 2026"
    assert created["description"] == "A study of review checklists."
    assert created["status"] == "active"
    assert created["brief"]["topic"] == BRIEF["topic"]
    assert created["brief"]["schema_version"] == 1
    assert created["policy_overrides"] == {"q_min": 80}
    # No runs yet: derived fields are honest zeros and nulls.
    assert created["run_count"] == 0
    assert created["latest_run_id"] is None
    assert created["best_q"] is None
    assert created["created_at"].endswith(("Z", "+00:00"))

    fetched = projects_client.get(f"/api/projects/{created['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json() == created

    listed = projects_client.get("/api/projects", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert body["projects"] == [created]


def test_create_rejects_a_duplicate_name_with_409(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    create_project(projects_client, headers, name="Same name")
    response = projects_client.post(
        "/api/projects", json={"name": "Same name", "brief": BRIEF}, headers=headers
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["context"]["reason"] == "project_exists"
    assert projects_client.get("/api/projects", headers=headers).json()["total"] == 1


def test_patch_changes_only_the_fields_it_names(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    created = create_project(projects_client, headers, description="first")

    patched = projects_client.patch(
        f"/api/projects/{created['id']}",
        json={"name": "Renamed", "description": None, "status": "archived"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "Renamed"
    assert body["description"] is None  # explicit null clears
    assert body["status"] == "archived"
    assert body["brief"] == created["brief"]  # untouched
    assert body["policy_overrides"] == {}

    new_brief = dict(BRIEF, target_words=6000)
    patched_again = projects_client.patch(
        f"/api/projects/{created['id']}", json={"brief": new_brief}, headers=headers
    )
    assert patched_again.status_code == 200
    assert patched_again.json()["brief"]["target_words"] == 6000


def test_patch_with_no_fields_is_422(projects_client: TestClient, headers: dict[str, str]) -> None:
    created = create_project(projects_client, headers)
    response = projects_client.patch(f"/api/projects/{created['id']}", json={}, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_rejects_a_null_name(projects_client: TestClient, headers: dict[str, str]) -> None:
    created = create_project(projects_client, headers)
    response = projects_client.patch(
        f"/api/projects/{created['id']}", json={"name": None}, headers=headers
    )
    assert response.status_code == 422, response.text
    errors = response.json()["error"]["context"]["errors"]
    assert [item["loc"] for item in errors] == ["name"]


def test_delete_removes_the_project(projects_client: TestClient, headers: dict[str, str]) -> None:
    created = create_project(projects_client, headers)

    deleted = projects_client.delete(f"/api/projects/{created['id']}", headers=headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert projects_client.get(f"/api/projects/{created['id']}", headers=headers).status_code == 404
    assert projects_client.get("/api/projects", headers=headers).json()["total"] == 0
    # Deleting twice is a 404, not a 500.
    assert (
        projects_client.delete(f"/api/projects/{created['id']}", headers=headers).status_code == 404
    )


# --------------------------------------------------------------------------- ownership
def test_another_users_project_is_invisible(projects_client: TestClient) -> None:
    owner = register(projects_client, "owner@example.org")
    intruder = register(projects_client, "intruder@example.org")
    created = create_project(projects_client, owner, name="Private")

    assert (
        projects_client.get(f"/api/projects/{created['id']}", headers=intruder).status_code == 404
    )
    assert (
        projects_client.patch(
            f"/api/projects/{created['id']}", json={"name": "stolen"}, headers=intruder
        ).status_code
        == 404
    )
    assert (
        projects_client.delete(f"/api/projects/{created['id']}", headers=intruder).status_code
        == 404
    )
    # The intruder's own list stays empty, and the owner's project is untouched.
    assert projects_client.get("/api/projects", headers=intruder).json()["total"] == 0
    still_there = projects_client.get(f"/api/projects/{created['id']}", headers=owner)
    assert still_there.status_code == 200
    assert still_there.json()["name"] == "Private"

    # Two owners may use the same name: uniqueness is per owner, not global.
    create_project(projects_client, intruder, name="Private")


def test_unknown_project_id_is_404(projects_client: TestClient, headers: dict[str, str]) -> None:
    assert (
        projects_client.get(f"/api/projects/{sample_ulid(7)}", headers=headers).status_code == 404
    )
    assert projects_client.get("/api/projects/not-a-ulid", headers=headers).status_code == 404


# --------------------------------------------------------------------------- pagination
def test_pagination_visits_every_project_exactly_once(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    created = [
        create_project(projects_client, headers, name=f"Project {index}") for index in range(5)
    ]

    seen: list[str] = []
    cursors: list[str | None] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = projects_client.get("/api/projects", params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 5
        assert len(body["projects"]) <= 2
        seen.extend(item["id"] for item in body["projects"])
        cursors.append(body["next_cursor"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            assert body["has_more"] is False
            break
        assert body["has_more"] is True
        assert pages < 10, "pagination did not terminate"

    assert pages == 3  # 2 + 2 + 1
    assert len(seen) == 5
    assert len(set(seen)) == 5
    assert set(seen) == {item["id"] for item in created}
    # Newest first: every page's created_at is non-increasing across the walk.
    timestamps = [
        item["created_at"]
        for item in sorted(created, key=lambda item: item["created_at"], reverse=True)
    ]
    assert timestamps == sorted(timestamps, reverse=True)
    assert cursors[0] is not None and cursors[1] is not None and cursors[2] is None


def test_limit_is_bounded(projects_client: TestClient, headers: dict[str, str]) -> None:
    create_project(projects_client, headers)
    assert projects_client.get("/api/projects?limit=0", headers=headers).status_code == 422
    assert projects_client.get("/api/projects?limit=201", headers=headers).status_code == 422
    assert projects_client.get("/api/projects?limit=1", headers=headers).json()["total"] == 1


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!!",
        "e30",  # base64('{}') - valid JSON, no sort key
        "W10",  # base64('[]') - not an object
        "eyJ0IjoiMjAyNi0xMC0yN1QwMDowMDowMCswMDowMCIsImlkIjoibm90LWEtdWxpZCJ9",
    ],
)
def test_invalid_cursor_is_a_422_on_the_cursor_field(
    projects_client: TestClient, headers: dict[str, str], cursor: str
) -> None:
    response = projects_client.get("/api/projects", params={"cursor": cursor}, headers=headers)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["context"]["errors"][0]["loc"] == "cursor"


# --------------------------------------------------------------------------- validation
def test_invalid_brief_reports_every_field_and_creates_nothing(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    canary = "quarterly-revenue-canary-9f3"
    bad_brief = dict(BRIEF)
    bad_brief.pop("target_words")
    bad_brief["citation_style"] = "MLA9"
    bad_brief["must_include"] = [canary]

    response = projects_client.post(
        "/api/projects", json={"name": "Bad", "brief": bad_brief}, headers=headers
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    locations = {item["loc"] for item in error["context"]["errors"]}
    assert "brief.target_words" in locations
    assert "brief.citation_style" in locations
    assert all({"loc", "msg", "type"} == set(item) for item in error["context"]["errors"])
    assert canary not in response.text  # never echo submitted values

    # A model-level rule (must_include vs must_avoid) is reported on the brief itself.
    contradictory = projects_client.post(
        "/api/projects",
        json={
            "name": "Contradiction",
            "brief": dict(BRIEF, must_include=[canary], must_avoid=[canary]),
        },
        headers=headers,
    )
    assert contradictory.status_code == 422, contradictory.text
    locations = {item["loc"] for item in contradictory.json()["error"]["context"]["errors"]}
    assert locations == {"brief"}
    assert canary not in contradictory.text

    assert projects_client.get("/api/projects", headers=headers).json()["total"] == 0


def test_invalid_policy_overrides_are_field_errors(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    unknown = projects_client.post(
        "/api/projects",
        json={"name": "Typo", "brief": BRIEF, "policy_overrides": {"q_mni": 80}},
        headers=headers,
    )
    assert unknown.status_code == 422, unknown.text
    errors = unknown.json()["error"]["context"]["errors"]
    assert errors[0]["loc"] == "policy_overrides.q_mni"
    assert errors[0]["type"] == "unknown_key"

    out_of_range = projects_client.post(
        "/api/projects",
        json={"name": "Range", "brief": BRIEF, "policy_overrides": {"q_min": 500}},
        headers=headers,
    )
    assert out_of_range.status_code == 422, out_of_range.text
    locations = {item["loc"] for item in out_of_range.json()["error"]["context"]["errors"]}
    assert "policy_overrides.q_min" in locations

    nested = projects_client.post(
        "/api/projects",
        json={"name": "Nested", "brief": BRIEF, "policy_overrides": {"plateau": {"min_gain": -1}}},
        headers=headers,
    )
    assert nested.status_code == 422
    locations = {item["loc"] for item in nested.json()["error"]["context"]["errors"]}
    assert "policy_overrides.plateau.min_gain" in locations

    assert projects_client.get("/api/projects", headers=headers).json()["total"] == 0


def test_patch_validates_the_brief_too(
    projects_client: TestClient, headers: dict[str, str]
) -> None:
    created = create_project(projects_client, headers)
    response = projects_client.patch(
        f"/api/projects/{created['id']}", json={"brief": {"topic": "only a topic"}}, headers=headers
    )
    assert response.status_code == 422, response.text
    locations = {item["loc"] for item in response.json()["error"]["context"]["errors"]}
    assert "brief.target_words" in locations
    unchanged = projects_client.get(f"/api/projects/{created['id']}", headers=headers)
    assert unchanged.json()["brief"] == created["brief"]


# --------------------------------------------------------------------------- 409 guard
def test_patch_and_delete_are_refused_while_a_run_is_active(
    projects_client: TestClient, headers: dict[str, str], runs: InMemoryRunsQuery
) -> None:
    created = create_project(projects_client, headers)
    run_id = sample_ulid(3)
    runs.add(
        RunFacts(
            id=run_id,
            owner_id=created["owner_id"],
            project_id=created["id"],
            state="EXECUTING",
            q=71,
        )
    )

    patched = projects_client.patch(
        f"/api/projects/{created['id']}", json={"name": "Mid-run edit"}, headers=headers
    )
    assert patched.status_code == 409, patched.text
    error = patched.json()["error"]
    assert error["code"] == "conflict"
    assert error["context"]["reason"] == "active_run"
    assert error["context"]["run_id"] == run_id
    assert error["context"]["state"] == "EXECUTING"

    deleted = projects_client.delete(f"/api/projects/{created['id']}", headers=headers)
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["error"]["context"]["reason"] == "active_run"

    # The project is still there and untouched.
    current = projects_client.get(f"/api/projects/{created['id']}", headers=headers)
    assert current.status_code == 200
    assert current.json()["name"] == created["name"]

    # Once the run settles, both operations work again.
    runs.advance(run_id, "DONE")
    assert (
        projects_client.patch(
            f"/api/projects/{created['id']}", json={"name": "After the run"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        projects_client.delete(f"/api/projects/{created['id']}", headers=headers).status_code == 204
    )


def test_checkpoint_is_not_an_active_run(
    projects_client: TestClient, headers: dict[str, str], runs: InMemoryRunsQuery
) -> None:
    """The loop is waiting for a human at CHECKPOINT: editing the project is legal."""
    created = create_project(projects_client, headers)
    runs.add(
        RunFacts(
            id=sample_ulid(4),
            owner_id=created["owner_id"],
            project_id=created["id"],
            state="CHECKPOINT",
            q=78,
        )
    )
    response = projects_client.patch(
        f"/api/projects/{created['id']}", json={"description": "edited"}, headers=headers
    )
    assert response.status_code == 200, response.text


def test_another_users_run_does_not_block_my_project(
    projects_client: TestClient, headers: dict[str, str], runs: InMemoryRunsQuery
) -> None:
    created = create_project(projects_client, headers)
    runs.add(
        RunFacts(
            id=sample_ulid(5),
            owner_id=sample_ulid(9),  # someone else's run, coincidentally same project id
            project_id=created["id"],
            state="EXECUTING",
        )
    )
    response = projects_client.patch(
        f"/api/projects/{created['id']}", json={"description": "mine"}, headers=headers
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- derived run facts
def test_project_view_reports_run_facts(
    projects_client: TestClient, headers: dict[str, str], runs: InMemoryRunsQuery
) -> None:
    created = create_project(projects_client, headers)
    owner_id = created["owner_id"]
    runs.add(
        RunFacts(
            id=sample_ulid(5),
            owner_id=owner_id,
            project_id=created["id"],
            state="STOPPED_BUDGET",
            q=66,
            best_q=66.0,
        )
    )
    runs.add(
        RunFacts(
            id=sample_ulid(6),
            owner_id=owner_id,
            project_id=created["id"],
            state="CHECKPOINT",
            q=78,
            best_q=78.0,
        )
    )
    view = projects_client.get(f"/api/projects/{created['id']}", headers=headers).json()
    assert view["run_count"] == 2
    assert view["latest_run_id"] == sample_ulid(6)
    assert view["latest_state"] == "CHECKPOINT"
    assert view["latest_q"] == 78
    assert view["best_q"] == 78.0


# --------------------------------------------------------------------------- no leaks
def test_brief_contents_never_reach_logs_or_the_audit_trail(
    projects_client: TestClient, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    canary = "provided-data-canary-7d1"
    caplog.set_level(logging.DEBUG)
    brief = dict(BRIEF, provided_data=[f"pilot run: {canary}"])
    created = create_project(
        projects_client,
        headers,
        brief=brief,
        policy_overrides={"q_min": 85, "plateau": {"min_gain": 2.0}},
    )
    projects_client.patch(
        f"/api/projects/{created['id']}", json={"brief": dict(brief)}, headers=headers
    )

    assert canary not in caplog.text
    # The stored brief does contain the data - this is about logs, not storage.
    assert projects_client.get(f"/api/projects/{created['id']}", headers=headers).json()["brief"][
        "provided_data"
    ] == [f"pilot run: {canary}"]

    factory = projects_client.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        rows = session.scalars(
            select(AuditLogRow).where(AuditLogRow.action == "project.create")
        ).all()
        assert len(rows) == 1
        detail = rows[0].detail or ""
    assert canary not in detail
    assert "policy_keys" in detail  # field names are audited, values are not


# --------------------------------------------------------------------------- wiring
def test_default_wiring_reads_projects_runs_from_the_store(default_client: TestClient) -> None:
    """Without an override, the seam is the app's real run store, read-only."""
    headers = register(default_client, "store@example.org")
    assert default_client.get("/api/projects", headers=headers).status_code == 200
    seam = default_client.app.state.store_runs_query  # type: ignore[attr-defined]
    assert isinstance(seam, StoreRunsQuery)
    assert seam.store is default_client.app.state.run_store  # type: ignore[attr-defined]
