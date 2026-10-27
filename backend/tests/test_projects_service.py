"""Projects domain layer: cursors, the runs seam, validation and page mechanics.

These tests stay below HTTP on purpose. The API suite proves the contract a client
sees; this one pins the pieces that are easy to get subtly wrong and expensive to
debug through a router: keyset cursor parsing, the "later record wins" rule of the
in-memory runs fake, the read-only adapter over the real ``RunStore``, and the exact
shape of a per-field validation error.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.orchestrator.persistence import InMemoryRunStore, RunRecord
from app.orchestrator.state_machine import RunStateName
from app.projects.pagination import decode_cursor, encode_cursor
from app.projects.runs import InMemoryRunsQuery, RunFacts, StoreRunsQuery
from app.projects.schemas import ProjectCreate, ProjectPatch
from app.projects.service import ProjectService
from app.projects.validation import (
    ProjectValidationError,
    validate_brief,
    validate_policy_overrides,
)
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.api

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def _opaque(payload: object) -> str:
    """base64url of a JSON document, with the padding stripped like a real cursor."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- cursors
def test_cursor_round_trips_an_aware_timestamp() -> None:
    cursor = encode_cursor(NOW, sample_ulid(1))
    decoded = decode_cursor(cursor)
    assert decoded.created_at == NOW
    assert decoded.id == sample_ulid(1)
    assert cursor.isascii()


def test_cursor_normalises_a_naive_timestamp_to_utc() -> None:
    decoded = decode_cursor(encode_cursor(datetime(2026, 10, 27, 12, 0, 0), sample_ulid(2)))
    assert decoded.created_at == NOW
    assert decoded.created_at.tzinfo is not None


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "   ",
        "not base64 !!!",
        _opaque([]),
        _opaque({}),
        _opaque({"t": "not-a-date", "id": sample_ulid(1)}),
        _opaque({"t": NOW.isoformat(), "id": "not-a-ulid"}),
        _opaque({"t": 42, "id": sample_ulid(1)}),
        "x" * 600,  # oversized
    ],
)
def test_cursor_rejects_malformed_input(cursor: str) -> None:
    with pytest.raises(ValueError):
        decode_cursor(cursor)


# --------------------------------------------------------------------------- in-memory seam
def test_in_memory_seam_picks_the_newest_running_run() -> None:
    owner, project = sample_ulid(0), sample_ulid(1)
    seam = InMemoryRunsQuery(
        [
            RunFacts(id=sample_ulid(2), owner_id=owner, project_id=project, state="DONE"),
            RunFacts(id=sample_ulid(3), owner_id=owner, project_id=project, state="PLANNING"),
            RunFacts(id=sample_ulid(4), owner_id=owner, project_id=project, state="EXECUTING"),
        ]
    )
    active = seam.active_run(owner, project)
    assert active is not None
    assert (active.id, active.state) == (sample_ulid(4), "EXECUTING")

    seam.advance(sample_ulid(4), RunStateName.DONE)
    assert seam.active_run(owner, project) is not None  # the PLANNING one is still live
    assert seam.active_run(owner, project).id == sample_ulid(3)  # type: ignore[union-attr]

    seam.advance(sample_ulid(3), RunStateName.CHECKPOINT)
    assert seam.active_run(owner, project) is None  # checkpoint waits for a human
    seam.clear()
    assert seam.summary(owner, project).run_count == 0


def test_in_memory_seam_is_owner_scoped_and_reports_q_facts() -> None:
    owner, other, project = sample_ulid(0), sample_ulid(1), sample_ulid(2)
    seam = InMemoryRunsQuery(
        [
            RunFacts(
                id=sample_ulid(3), owner_id=owner, project_id=project, state="DONE", q=61, best_q=61
            ),
            RunFacts(
                id=sample_ulid(4),
                owner_id=owner,
                project_id=project,
                state="CHECKPOINT",
                q=78,
                best_q=74.5,
            ),
            RunFacts(id=sample_ulid(5), owner_id=other, project_id=project, state="EXECUTING"),
            RunFacts(id=sample_ulid(6), owner_id=owner, project_id=sample_ulid(7), state="DONE"),
        ]
    )
    summary = seam.summary(owner, project)
    assert summary.run_count == 2
    assert summary.latest_run_id == sample_ulid(4)
    assert summary.latest_state == "CHECKPOINT"
    assert summary.latest_q == 78
    assert summary.best_q == 74.5
    assert seam.active_run(owner, project) is None
    assert seam.active_run(other, project) is not None


def test_store_seam_reads_the_real_run_store() -> None:
    owner, other, project = sample_ulid(0), sample_ulid(1), sample_ulid(2)
    store = InMemoryRunStore()
    store.save(
        RunRecord(id=sample_ulid(3), project_id=project, owner_id=owner, state=RunStateName.DONE)
    )
    store.save(
        RunRecord(
            id=sample_ulid(4),
            project_id=project,
            owner_id=owner,
            state=RunStateName.EXECUTING,
            best_q=71.5,
        )
    )
    store.save(  # another owner, same project id: must not leak into the facts
        RunRecord(
            id=sample_ulid(5),
            project_id=project,
            owner_id=other,
            state=RunStateName.EXECUTING,
        )
    )
    store.save(  # another project: must not be counted
        RunRecord(id=sample_ulid(6), project_id=sample_ulid(7), owner_id=owner)
    )

    seam = StoreRunsQuery(store)

    active = seam.active_run(owner, project)
    assert active is not None
    assert (active.id, active.state) == (sample_ulid(4), "EXECUTING")

    summary = seam.summary(owner, project)
    assert summary.run_count == 2
    assert summary.latest_run_id == sample_ulid(4)
    assert summary.latest_state == "EXECUTING"
    assert summary.latest_q is None  # no QualityReport yet
    assert summary.best_q == 71.5

    store.save(
        RunRecord(id=sample_ulid(4), project_id=project, owner_id=owner, state=RunStateName.DONE)
    )
    assert seam.active_run(owner, project) is None


# --------------------------------------------------------------------------- validation
def test_valid_brief_is_returned_json_safe() -> None:
    stored = validate_brief(dict(SAMPLE_BRIEF))
    assert stored["topic"] == SAMPLE_BRIEF["topic"]
    assert stored["schema_version"] == 1
    assert stored["target_words"] == SAMPLE_BRIEF["target_words"]


def test_invalid_brief_errors_name_each_field_and_never_echo_values() -> None:
    payload = dict(SAMPLE_BRIEF, citation_style="MLA9", must_include=["kept"], must_avoid=["kept"])
    with pytest.raises(ProjectValidationError) as caught:
        validate_brief(payload)
    error = caught.value
    assert error.code == "validation_error"
    assert error.http_status == 422
    locations = {item["loc"] for item in error.context["errors"]}
    assert "brief.citation_style" in locations
    # "kept" is too short to scrub; a distinctive value must not be echoed.
    long_value = "a distinctive quoted claim nobody should see twice"
    with pytest.raises(ProjectValidationError) as caught_long:
        validate_brief(dict(SAMPLE_BRIEF, must_include=[long_value], must_avoid=[long_value]))
    assert long_value not in caught_long.value.message
    assert "[REDACTED]" in caught_long.value.message
    assert caught_long.value.context["errors"][0]["loc"] == "brief"


def test_policy_overrides_are_validated_and_stored_verbatim() -> None:
    overrides = {"q_min": 85, "plateau": {"min_gain": 2.0}}
    assert validate_policy_overrides(overrides) == overrides
    assert validate_policy_overrides({}) == {}
    assert validate_policy_overrides(None) == {}


def test_policy_overrides_reject_unknown_keys_and_bad_values() -> None:
    with pytest.raises(ProjectValidationError) as unknown:
        validate_policy_overrides({"q_mni": 80})
    assert unknown.value.context["errors"][0]["loc"] == "policy_overrides.q_mni"

    with pytest.raises(ProjectValidationError) as bad_value:
        validate_policy_overrides({"max_iterations": 0})
    assert bad_value.value.context["errors"][0]["loc"] == "policy_overrides.max_iterations"

    with pytest.raises(ProjectValidationError) as bad_nesting:
        validate_policy_overrides({"plateau": {"min_gain": 2.0, "window": 1}})
    assert bad_nesting.value.context["errors"][0]["loc"] == "policy_overrides.plateau.window"

    with pytest.raises(ProjectValidationError) as not_an_object:
        validate_policy_overrides([("q_min", 80)])
    assert not_an_object.value.context["errors"][0]["loc"] == "policy_overrides"


# --------------------------------------------------------------------------- service
@pytest.fixture
def service(session, user_row) -> ProjectService:  # type: ignore[no-untyped-def]
    """Project service over the test session, with the empty in-memory runs seam."""
    return ProjectService(session, InMemoryRunsQuery())


def payload(name: str = "Peer review") -> ProjectCreate:
    return ProjectCreate(name=name, brief=dict(SAMPLE_BRIEF))


def test_service_pages_newest_first_and_clamps_the_limit(
    service: ProjectService,
    session,
    user_row,  # type: ignore[no-untyped-def]
) -> None:
    owner = user_row.id
    created = [service.create(owner, payload(f"Project {index}")) for index in range(3)]
    session.commit()

    largest = service.list_page(owner, limit=10_000)
    assert largest.total == 3
    assert [view.name for view in largest.projects] == ["Project 2", "Project 1", "Project 0"]
    assert largest.has_more is False
    assert largest.next_cursor is None

    first = service.list_page(owner, limit=0)  # clamped to 1, not an empty page
    assert len(first.projects) == 1
    assert first.has_more is True
    assert first.next_cursor is not None

    second = service.list_page(owner, limit=2, cursor=first.next_cursor)
    assert [view.id for view in second.projects] == [created[1].id, created[0].id]
    assert second.has_more is False
    assert second.next_cursor is None
    assert second.total == 3  # total is the whole list, not the page


def test_service_enforces_ownership_and_unique_names(
    service: ProjectService,
    session,
    user_row,  # type: ignore[no-untyped-def]
) -> None:
    owner = user_row.id
    created = service.create(owner, payload())
    session.commit()

    with pytest.raises(NotFoundError):
        service.get(sample_ulid(9), created.id)
    with pytest.raises(NotFoundError):
        service.patch(sample_ulid(9), created.id, ProjectPatch(name="Renamed"))
    with pytest.raises(NotFoundError):
        service.delete(sample_ulid(9), created.id)
    with pytest.raises(ConflictError):
        service.create(owner, payload())
    assert service.list_page(sample_ulid(9)).total == 0

    # The owner may still rename their own project, and the name is per owner.
    renamed = service.patch(owner, created.id, ProjectPatch(name="Renamed"))
    assert renamed.name == "Renamed"
    session.commit()


def test_summaries_answer_a_page_in_one_pass() -> None:
    """The seam groups several projects at once; empty projects are simply absent."""
    owner, project_a, project_b = sample_ulid(0), sample_ulid(1), sample_ulid(2)
    seam = InMemoryRunsQuery(
        [
            RunFacts(id=sample_ulid(3), owner_id=owner, project_id=project_a, state="DONE", q=70),
            RunFacts(
                id=sample_ulid(4), owner_id=owner, project_id=project_b, state="EXECUTING", q=55
            ),
        ]
    )
    facts = seam.summaries(owner, [project_a, project_b, sample_ulid(5)])
    assert set(facts) == {project_a, project_b}
    assert facts[project_a].run_count == 1
    assert facts[project_a].latest_state == "DONE"
    assert facts[project_b].latest_q == 55
    assert facts[project_a] == seam.summary(owner, project_a)


def test_store_summaries_group_several_projects() -> None:
    owner, project_a, project_b = sample_ulid(0), sample_ulid(1), sample_ulid(2)
    store = InMemoryRunStore()
    store.save(
        RunRecord(
            id=sample_ulid(3),
            project_id=project_a,
            owner_id=owner,
            state=RunStateName.DONE,
        )
    )
    store.save(
        RunRecord(
            id=sample_ulid(4),
            project_id=project_b,
            owner_id=owner,
            state=RunStateName.STOPPED_BUDGET,
            best_q=66.0,
        )
    )
    facts = StoreRunsQuery(store).summaries(owner, [project_a, project_b])
    assert set(facts) == {project_a, project_b}
    assert facts[project_a].latest_state == "DONE"
    assert facts[project_b].latest_state == "STOPPED_BUDGET"
    assert facts[project_b].best_q == 66.0


def test_list_page_reads_the_runs_seam_once_per_page(
    session,
    user_row,  # type: ignore[no-untyped-def]
) -> None:
    """A 50-project page must not become 50 full scans of the run store."""

    class CountingSeam(InMemoryRunsQuery):
        summary_calls = 0
        summaries_calls = 0

        def summary(self, owner_id: str, project_id: str):  # type: ignore[no-untyped-def]
            CountingSeam.summary_calls += 1
            return super().summary(owner_id, project_id)

        def summaries(self, owner_id: str, project_ids):  # type: ignore[no-untyped-def]
            CountingSeam.summaries_calls += 1
            return super().summaries(owner_id, project_ids)

    service = ProjectService(session, CountingSeam())
    for index in range(3):
        service.create(user_row.id, payload(f"Project {index}"))
    session.commit()

    # The creates each built one view (three single reads); reset and read a page.
    CountingSeam.summary_calls = 0
    CountingSeam.summaries_calls = 0

    page = service.list_page(user_row.id, limit=10)
    assert len(page.projects) == 3
    assert CountingSeam.summaries_calls == 1
    assert CountingSeam.summary_calls == 0
