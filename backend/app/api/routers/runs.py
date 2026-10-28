"""Run API: create, decide, stop, inspect (Step 9b, item 6).

Endpoints (all under the house ``/api`` prefix, all authenticated):

* ``GET /api/projects/{project_id}/runs`` - the project's run history,
  newest first, summaries only (no brief, no plan, no bodies).
* ``POST /api/projects/{project_id}/runs`` - intake + start. Body carries
  either ``brief`` (a raw CONTRACTS v1 Brief object; intake names any
  missing fields with 409 ``needs_user_input``) or ``quick_edit`` (the
  planner-free cheap path). Runs execute in the background unless
  ``wait=true``, which awaits the next checkpoint/stop inline - the
  deterministic mode tests and scripts use.
* ``POST /api/runs/{id}/decision`` - a checkpoint/stop choice
  (``finish|continue|rollback|edit_goals|add_budget|continue_anyway``).
* ``POST /api/runs/{id}/stop`` - request stop; finalises immediately when
  idle, flags the run when executing (the loop stops at the next wave).
* ``GET /api/runs/{id}`` - run state, plan, decisions, stop report.
* ``GET /api/runs/{id}/events`` - Server-Sent Events over the persisted
  event log (replay + live tail; ``?once=true`` replays and closes).
* ``GET /api/runs/{id}/events/history`` - the same log, pageable as JSON.
* ``GET /api/runs/{id}/digest`` - the bounded ``claude.md`` digest.
* ``GET /api/runs/{id}/ledger`` - append-only change entries.
* ``GET /api/runs/{id}/quality`` - latest quality report + Q series.
* ``GET /api/runs/{id}/article`` - assembled markdown + sections + sources.
* ``GET /api/runs/{id}/article/sections/{sid}/history`` - version metadata.
* ``GET /api/runs/{id}/article/sections/{sid}/diff`` - unified diff + stats.
* ``GET /api/runs/{id}/article/snapshots`` - whole-article snapshots.
* ``POST /api/runs/{id}/article/restore`` - rollback to a snapshot
  (``dry_run=true`` previews it; refuses while a wave is executing).
* ``GET /api/runs/{id}/usage`` - token usage by role/iteration + planner.

A project's run list is owner-scoped: another owner's runs are simply absent, and a
label that neither exists as a stored project nor has a single run reads as 404.

While a run of the same (owner, project) is live - executing or paused at a
checkpoint - a second ``POST`` is refused with ``409 run_conflict`` and
``context.reason=active_run`` (the frozen OpenAPI design: one project, one
live run).

There is no Project *contract* in CONTRACTS v1 (see the Step 9 handoff): a
``project_id`` is an opaque owner-scoped label stored on the run record. Since
Step 11 there is a project *table* (``app.projects``, ``/api/projects``), so a run
created under a stored project carries that project's ULID as its ``project_id``;
a legacy slug still works, because this router never required a row to exist.
Runs of another owner read as 404.

Per-owner wiring: the planner/agents resolve the *caller's* bindings at call
time through ``UserBindingResolver`` over the account store, and provider
calls go through the existing ``ProviderPool`` - so an account the user adds,
rotates or disables between two tasks is honoured by the very next call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict, Field, model_validator

from app.accounts.service import SqlAccountResolver
from app.agents.binding import UserBindingResolver
from app.agents.governor import CallBudgetGovernor
from app.agents.pool import PoolAgentProvider
from app.agents.registry import AgentRegistry
from app.agents.roles.factory import register_role_agents
from app.api.deps import (
    CurrentUserDep,
    ProjectServiceDep,
    get_policies_dep,
    get_pool,
    get_rubric_dep,
)
from app.api.schemas import ErrorEnvelope, ValidationErrorEnvelope
from app.contracts.brief import Brief
from app.contracts.common import Contract, SectionId, Ulid
from app.contracts.enums import SectionStatus
from app.contracts.ledger import ChangeEntry
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.verdict import Verdict
from app.orchestrator.intake import IntakeKind, QuickEditRequest
from app.orchestrator.persistence import FileRunStore, RunRecord
from app.orchestrator.runner import Orchestrator, PlannerServiceAdapter, RunManager
from app.orchestrator.state_machine import (
    RUNNING_STATES,
    TERMINAL_STATES,
    CheckpointKind,
    RunEvent,
    RunStateName,
)
from app.orchestrator.stop import OnComplete, StopReport, UserDecision
from app.planner.context import build_planner_contexts
from app.planner.service import PlannerService
from app.skills.loader import SkillLoader
from app.tools.base import ToolRegistry
from app.tools.registry import build_tool_registry

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["runs"])

_SSE_POLL_SECONDS = 0.25
# Planner completion ceiling when wiring contexts; the binding re-caps per call.
_PLANNER_MAX_OUTPUT_TOKENS = 4_096


# --------------------------------------------------------------------------- schemas
class RunKind(StrEnum):
    """``RunView.kind`` / ``RunSummary.kind`` - the spec's ``RunKind``.

    Spelled here rather than reusing the orchestrator's ``IntakeKind`` so the served
    OpenAPI component carries the name a generated client expects (``RunKind``). The
    two vocabularies are identical; :meth:`of` is the only conversion.
    """

    BRIEF = "brief"
    QUICK_EDIT = "quick_edit"

    @classmethod
    def of(cls, kind: IntakeKind) -> RunKind:
        """The wire spelling of an intake kind."""
        return cls(kind.value)


class RunCreate(Contract):
    """POST /projects/{id}/runs. Exactly one of ``brief`` / ``quick_edit``.

    ``brief`` is a plain JSON object rather than :class:`Brief` on purpose: intake
    validates it field by field so a brief that is missing something only the user can
    supply answers the spec's ``409 needs_user_input`` naming every gap - not a 422
    that hides which field is missing.
    """

    brief: dict[str, Any] | None = None
    quick_edit: QuickEditRequest | None = None
    on_complete: OnComplete = OnComplete.ASK
    max_wall_seconds: float | None = Field(default=None, gt=0.0, le=7 * 24 * 3600)
    wait: bool = Field(
        default=False, description="Run inline to the next checkpoint/stop (tests, scripts)."
    )

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> RunCreate:
        if (self.brief is None) == (self.quick_edit is None):
            raise ValueError("provide exactly one of 'brief' or 'quick_edit'")
        return self


class RunView(Contract):
    """A run as the API exposes it. Article bodies live on /article."""

    id: Ulid
    project_id: str
    owner_id: str | None = None
    kind: RunKind
    state: RunStateName
    iter: int
    brief: Brief | None = None
    plan: Plan | None = None
    goal_override: str | None = None
    decisions: list[LoopDecision] = Field(default_factory=list)
    q_series: list[int] = Field(default_factory=list)
    best_q: float | None = None
    best_snapshot_id: str | None = None
    stop_report: StopReport | None = None
    verdict: Verdict | None = None
    checkpoint_kind: CheckpointKind | None = None
    on_complete: OnComplete = OnComplete.ASK
    user_stop_requested: bool = False
    running: bool = False
    tokens_spent: int = 0
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: RunRecord, *, running: bool) -> RunView:
        """Project a persisted record onto the API view."""
        return cls(
            id=record.id,
            project_id=record.project_id,
            owner_id=record.owner_id,
            kind=RunKind.of(record.kind),
            state=record.state,
            iter=record.iter,
            brief=record.brief,
            plan=record.plan,
            goal_override=record.goal_override,
            decisions=list(record.decisions),
            q_series=record.q_series(),
            best_q=record.best_q,
            best_snapshot_id=record.best_snapshot_id,
            stop_report=record.stop_report,
            verdict=record.verdict,
            checkpoint_kind=record.checkpoint_kind,
            on_complete=record.on_complete,
            user_stop_requested=record.user_stop_requested,
            running=running,
            tokens_spent=record.total_tokens_spent(),
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class RunSummary(Contract):
    """One row of ``GET /projects/{id}/runs``.

    State and cost only: no brief, no plan, no decisions, no bodies. The list is
    what a project page polls, so it stays small even for a project with two
    hundred runs. Deliberately *not* :class:`app.projects.runs.RunSummary` - that
    one aggregates a project's runs for the project view; this one is one run.
    """

    id: Ulid
    project_id: str
    kind: RunKind
    state: RunStateName
    iter: int = Field(ge=0)
    best_q: float | None = None
    checkpoint_kind: CheckpointKind | None = None
    running: bool = False
    tokens_spent: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: RunRecord, *, running: bool) -> RunSummary:
        """Project a persisted record onto one list row."""
        return cls(
            id=record.id,
            project_id=record.project_id,
            kind=RunKind.of(record.kind),
            state=record.state,
            iter=record.iter,
            best_q=record.best_q,
            checkpoint_kind=record.checkpoint_kind,
            running=running,
            tokens_spent=record.total_tokens_spent(),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class RunList(Contract):
    """GET /projects/{id}/runs - the page plus the unpaged total."""

    runs: list[RunSummary] = Field(default_factory=list)
    total: int = Field(default=0, ge=0, description="Every run of this project, not just the page.")


class LedgerView(Contract):
    """GET /runs/{id}/ledger."""

    run_id: Ulid
    entries: list[ChangeEntry] = Field(default_factory=list)


class DigestView(Contract):
    """GET /runs/{id}/digest - the bounded claude.md the Planner reads."""

    run_id: Ulid
    markdown: str
    tokens: int
    max_tokens: int
    compacted: bool = False
    latest_q: int | None = None
    open_issues: list[str] = Field(default_factory=list)


class RunEventList(Contract):
    """GET /runs/{id}/events/history - the SSE log, pageable as JSON."""

    run_id: Ulid
    events: list[RunEvent] = Field(default_factory=list)
    next_after: int | None = Field(
        default=None, description="Cursor for the next page; null = log exhausted."
    )


class QualityPoint(Contract):
    """One entry of the Q series (the erosion detectors read the same series)."""

    iter: int = Field(ge=0)
    q: int = Field(ge=0, le=100)
    gates_passed: bool
    verified_citation_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class QualityView(Contract):
    """GET /runs/{id}/quality - latest report plus the Q history."""

    run_id: Ulid
    latest: QualityReport | None = None
    series: list[QualityPoint] = Field(default_factory=list)
    best_q: float | None = None


_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"


class VersionInfo(Contract):
    """Text-free metadata about one immutable section version."""

    section_id: SectionId
    version: int = Field(ge=0)
    hash: str = Field(pattern=_HASH_PATTERN)
    words: int = Field(ge=0)
    status: SectionStatus
    chars_added: int = Field(ge=0)
    chars_removed: int = Field(ge=0)
    patch_id: str | None = None
    entry_id: str | None = None
    created_at: datetime
    q_after: float | None = None


class SectionHistoryView(Contract):
    """GET /runs/{id}/article/sections/{sid}/history."""

    run_id: Ulid
    section_id: SectionId
    versions: list[VersionInfo] = Field(default_factory=list)


class DiffStats(Contract):
    """Character-level change size - the numbers the erosion detectors use."""

    chars_added: int = Field(ge=0)
    chars_removed: int = Field(ge=0)
    net_chars: int
    touched_chars: int = Field(ge=0)


class TextDiff(Contract):
    """A rendered diff between two section bodies (empty ``unified`` = no change)."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, str_strip_whitespace=False, str_min_length=0
    )

    stats: DiffStats
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    unified: str = ""


class SectionVersionRef(Contract):
    """The ``from``/``to`` side of a section diff: a version by reference."""

    version: int = Field(ge=0)
    hash: str = Field(pattern=_HASH_PATTERN)
    words: int | None = Field(default=None, ge=0)


class SectionDiffView(Contract):
    """GET /runs/{id}/article/sections/{sid}/diff. ``from`` is a JSON key."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True, validate_assignment=True
    )

    run_id: Ulid
    section_id: SectionId
    from_: SectionVersionRef = Field(alias="from")
    to: SectionVersionRef
    diff: TextDiff


class SnapshotSummary(Contract):
    """Metadata of one whole-article snapshot - no bodies."""

    id: str
    label: str
    q: float | None = None
    iter: int | None = None
    created_at: datetime
    section_ids: list[SectionId] = Field(default_factory=list)
    total_words: int = Field(ge=0)


class SnapshotList(Contract):
    """GET /runs/{id}/article/snapshots - newest first."""

    run_id: Ulid
    best_snapshot_id: str | None = None
    snapshots: list[SnapshotSummary] = Field(default_factory=list)


class RestoreRequest(Contract):
    """POST /runs/{id}/article/restore body."""

    snapshot_id: str = Field(min_length=1)
    dry_run: bool = Field(
        default=False, description="Report what would change without writing a new version."
    )


class RestoreView(Contract):
    """POST /runs/{id}/article/restore - what the restore changed."""

    run_id: Ulid
    snapshot_id: str
    label: str
    q: float | None = None
    restored: list[SectionId] = Field(default_factory=list)
    unchanged: list[SectionId] = Field(default_factory=list)
    versions: dict[str, int] = Field(default_factory=dict)
    state: RunStateName
    updated_at: datetime
    dry_run: bool = False


class ArticleSection(SectionState):
    """A SectionState plus its exact body (spec: SectionState + ``text``).

    Section bodies are payload, not metadata: no whitespace stripping, and the
    empty string is the legal body of an ``EMPTY`` section.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
        str_min_length=0,
    )

    text: str = Field(default="")


class ArticleView(Contract):
    """GET /runs/{id}/article - assembled markdown, sections, sources."""

    run_id: Ulid
    markdown: str
    body_words: int = Field(ge=0)
    words_by_section: dict[str, int] = Field(default_factory=dict)
    sections: list[ArticleSection] = Field(default_factory=list)
    sources: list[SourceCard] = Field(default_factory=list)


class UsageBreakdown(Contract):
    """Tokens in one bucket. Wire shape: ``{"in", "out", "cached", "total"}``."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True, validate_assignment=True
    )

    in_: int = Field(alias="in", ge=0)
    out: int = Field(ge=0)
    cached: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


class RunUsageView(Contract):
    """GET /runs/{id}/usage - spend by role/iteration against the caps."""

    run_id: Ulid
    total: int = Field(ge=0)
    agents_total: UsageBreakdown
    planner: UsageBreakdown
    by_role: dict[str, UsageBreakdown] = Field(default_factory=dict)
    by_iter: dict[str, UsageBreakdown] = Field(default_factory=dict)
    token_cap: int = Field(ge=0)
    iterations: int = Field(ge=0)
    max_iterations: int = Field(ge=0)


# --------------------------------------------------------------------------- wiring
def _run_store(request: Request) -> FileRunStore:
    store: FileRunStore | None = getattr(request.app.state, "run_store", None)
    if store is None:  # pragma: no cover - lifespan always builds one
        settings = request.app.state.settings
        store = FileRunStore(settings.runs_path)
        request.app.state.run_store = store
    return store


def _tool_registry(request: Request) -> ToolRegistry:
    """The Step 6 tool set, built once per app (pure, deterministic, no credentials)."""
    tools: ToolRegistry | None = getattr(request.app.state, "tool_registry", None)
    if tools is None:
        tools = build_tool_registry()
        request.app.state.tool_registry = tools
    return tools


def _manager_for(request: Request, user_id: str) -> RunManager:
    """The caller's run manager, built once per (app, user) and cached.

    The orchestrator itself holds no credentials: bindings resolve per call
    through the account store, so caching the manager cannot go stale.
    """
    managers: dict[str, RunManager] = getattr(request.app.state, "run_managers", {})
    request.app.state.run_managers = managers
    cached = managers.get(user_id)
    if cached is not None:
        return cached

    settings = request.app.state.settings
    policies = get_policies_dep(request)
    rubric = get_rubric_dep(request)
    account_resolver = SqlAccountResolver(
        request.app.state.session_factory, request.app.state.encryptor
    )
    bindings = UserBindingResolver(account_resolver, user_id=user_id)
    provider = PoolAgentProvider(get_pool(request), user_id=user_id)
    governor = CallBudgetGovernor(policies)
    skills = SkillLoader(settings.skills_path)

    registry = AgentRegistry(bindings=bindings)
    # One wiring path for the five roles: root rules + role rules + role prompt, the
    # deterministic Step 6 tools, the rubric, and a scoreboard the reviewer writes and
    # the validator reads. Run-scoped section *text* is not available at wiring time
    # (it lives in the run's article store), so a role that must see the text refuses
    # with a ConfigError naming the provider rather than reviewing an empty article.
    register_role_agents(
        registry,
        provider=provider,
        bindings=bindings,
        governor=governor,
        skills=skills,
        rules_dir=settings.rules_path,
        rubric=rubric,
        tools=_tool_registry(request),
    )

    planner_service = PlannerService(
        provider=provider,
        bindings=bindings,
        governor=governor,
        contexts=build_planner_contexts(
            rules_dir=settings.rules_path,
            rubric=rubric,
            skills=skills,
            max_input_tokens=policies.call_input_cap,
            max_output_tokens=_PLANNER_MAX_OUTPUT_TOKENS,
        ),
        rubric=rubric,
        policies=policies,
    )
    orchestrator = Orchestrator(
        PlannerServiceAdapter(planner_service, rubric),
        registry,
        policies=policies,
        rubric=rubric,
        store=_run_store(request),
        binding_check=lambda task: registry.binding_for(task.role),
    )
    manager = RunManager(orchestrator)
    managers[user_id] = manager
    return manager


def _view(manager: RunManager, record: RunRecord) -> RunView:
    return RunView.from_record(record, running=manager.is_running(record.id))


# --------------------------------------------------------------------------- responses
#: Documented failure modes, mirroring the frozen ``docs/openapi/openapi.yaml``.
#: The handlers in ``app.api.main`` emit these bodies at runtime; declaring them here
#: makes the served ``/openapi.json`` tell a generated client the same story as the
#: spec instead of stopping at "200 or FastAPI's own validation error".
_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorEnvelope,
        "description": "Missing, malformed, expired or unknown bearer token.",
    }
}
_RUN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorEnvelope,
        "description": (
            "No such run for this caller. Never 403 - the API does not confirm that a "
            "foreign id exists."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorEnvelope,
        "description": "No such object, or it belongs to somebody else.",
    }
}
_CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorEnvelope,
        "description": (
            "Well-formed request the current state refuses; branch on "
            "error.context.reason (invalid_transition, restore_while_running, ...)."
        ),
    }
}
_CREATE_CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorEnvelope,
        "description": (
            "Two conflicts share this status: needs_user_input when intake needs fields "
            "only the user can supply, and run_conflict with context.reason=active_run "
            "when the project already has a live run."
        ),
    }
}
_INVALID_BODY: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ValidationErrorEnvelope,
        "description": (
            "A body field failed validation. Locations and messages are reported; the "
            "submitted value is never echoed back."
        ),
    }
}


# --------------------------------------------------------------------------- endpoints
#: The OpenAPI ``ProjectId`` path parameter, plus the project table's ULID.
#:
#: The frozen spec (``components.parameters.ProjectId``) allows a lowercase slug,
#: ``^[a-z0-9][a-z0-9-]{2,63}$``, because when it was written a project *was* an opaque
#: label on a run. Step 11 added a real ``projects`` table whose ids are ULIDs (see its
#: ``spec-change-requests.md`` #3: "``ProjectId`` should become a ULID"), and the
#: console creates runs with ``project.id``. Accepting **both** is the only shape that
#: keeps every spec-legal label working *and* makes a stored project addressable: it is
#: a strict superset of the spec, so no request the spec allows is refused.
PROJECT_ID_PATTERN = r"^(?:[a-z0-9][a-z0-9-]{2,63}|[0-7][0-9A-HJKMNP-TV-Z]{25})$"
ProjectIdPath = Annotated[str, Path(pattern=PROJECT_ID_PATTERN)]


@router.get(
    "/projects/{project_id}/runs",
    response_model=RunList,
    summary="Runs of one project, newest first",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def list_project_runs(
    project_id: ProjectIdPath,
    request: Request,
    user: CurrentUserDep,
    projects: ProjectServiceDep,
    limit: int = Query(default=50, ge=1, le=200, description="Maximum runs to return."),
) -> RunList:
    """The run history of one project: summaries only, newest first.

    A project is addressable when it exists as a stored project (Step 11) *or* has
    at least one run - the opaque-label case the runs API has always supported, so a
    pre-Step-11 project does not disappear from the console. Anything else, including
    another owner's project, reads as 404 rather than an empty page that would confirm
    the label exists.
    """
    manager = _manager_for(request, user.id)
    records, total = manager.orchestrator.project_runs(project_id, owner_id=user.id, limit=limit)
    if total == 0:
        projects.get(user.id, project_id)  # 404 not_found when the project is unknown
    return RunList(
        runs=[
            RunSummary.from_record(record, running=manager.is_running(record.id))
            for record in records
        ],
        total=total,
    )


@router.post(
    "/projects/{project_id}/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create and start a run",
    responses={**_UNAUTHORIZED, **_CREATE_CONFLICT, **_INVALID_BODY},
)
async def create_run(
    project_id: ProjectIdPath, payload: RunCreate, request: Request, user: CurrentUserDep
) -> RunView:
    manager = _manager_for(request, user.id)
    orchestrator = manager.orchestrator
    body: dict[str, Any] = (
        dict(payload.brief)
        if payload.brief is not None
        else {"kind": "quick_edit", **payload.quick_edit.model_dump(mode="json")}  # type: ignore[union-attr]
    )
    record = orchestrator.create_run(
        project_id,
        body,
        owner_id=user.id,
        on_complete=payload.on_complete,
        max_wall_seconds=payload.max_wall_seconds,
    )
    if payload.wait:
        record = await orchestrator.run(record.id)
    else:
        manager.start(record.id)
    return _view(manager, record)


@router.post(
    "/runs/{run_id}/decision",
    response_model=RunView,
    summary="Answer a checkpoint",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND, **_CONFLICT, **_INVALID_BODY},
)
async def decide(
    run_id: str, payload: UserDecision, request: Request, user: CurrentUserDep
) -> RunView:
    manager = _manager_for(request, user.id)
    orchestrator = manager.orchestrator
    record = await orchestrator.apply_decision(run_id, payload, owner_id=user.id)
    if record.state in RUNNING_STATES:
        if payload.wait:
            record = await orchestrator.run(record.id)
        else:
            manager.start(record.id)
    return _view(manager, record)


@router.post(
    "/runs/{run_id}/stop",
    response_model=RunView,
    summary="Stop a run",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def stop_run(run_id: str, request: Request, user: CurrentUserDep) -> RunView:
    manager = _manager_for(request, user.id)
    record = await manager.orchestrator.request_stop(run_id, owner_id=user.id)
    return _view(manager, record)


@router.get(
    "/runs/{run_id}",
    response_model=RunView,
    summary="Run state",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_run(run_id: str, request: Request, user: CurrentUserDep) -> RunView:
    manager = _manager_for(request, user.id)
    return _view(manager, manager.orchestrator.get_record(run_id, owner_id=user.id))


async def _event_stream(
    orchestrator: Orchestrator, run_id: str, *, after_seq: int, once: bool
) -> AsyncIterator[str]:
    """Replay persisted events, then tail until the run pauses/stops."""
    seq = after_seq
    while True:
        events: list[RunEvent] = orchestrator.events_after(run_id, after_seq=seq)
        for event in events:
            seq = event.seq
            data = event.model_dump_json(by_alias=True)
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"
        if once:
            return
        record = orchestrator.get_record(run_id)
        settled = record.state in TERMINAL_STATES or record.state is RunStateName.CHECKPOINT
        if settled and not events:
            return
        if not events:
            await asyncio.sleep(_SSE_POLL_SECONDS)


@router.get(
    "/runs/{run_id}/events",
    summary="Run events (SSE)",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def run_events(
    run_id: str,
    request: Request,
    user: CurrentUserDep,
    after: int = Query(default=-1, ge=-1),
    once: bool = Query(default=False),
) -> StreamingResponse:
    manager = _manager_for(request, user.id)
    orchestrator = manager.orchestrator
    orchestrator.get_record(run_id, owner_id=user.id)  # 404 before streaming starts
    return StreamingResponse(
        _event_stream(orchestrator, run_id, after_seq=after, once=once),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/runs/{run_id}/events/history",
    response_model=RunEventList,
    summary="Run events as JSON (the SSE log, without the stream)",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def run_event_history(
    run_id: str,
    request: Request,
    user: CurrentUserDep,
    after: int = Query(default=-1, ge=-1),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return manager.orchestrator.events_page(run_id, after=after, limit=limit, owner_id=user.id)


@router.get(
    "/runs/{run_id}/digest",
    response_model=DigestView,
    summary="Bounded digest",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_digest(run_id: str, request: Request, user: CurrentUserDep) -> DigestView:
    manager = _manager_for(request, user.id)
    snapshot = await manager.orchestrator.digest_snapshot(run_id, owner_id=user.id)
    return DigestView(
        run_id=run_id,
        markdown=snapshot.markdown,
        tokens=snapshot.tokens,
        max_tokens=snapshot.max_tokens,
        compacted=snapshot.compacted,
        latest_q=snapshot.latest_q,
        open_issues=list(snapshot.open_issues),
    )


@router.get(
    "/runs/{run_id}/ledger",
    response_model=LedgerView,
    summary="Change ledger",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_ledger(run_id: str, request: Request, user: CurrentUserDep) -> LedgerView:
    manager = _manager_for(request, user.id)
    record = manager.orchestrator.get_record(run_id, owner_id=user.id)
    return LedgerView(run_id=run_id, entries=list(record.entries))


@router.get(
    "/runs/{run_id}/quality",
    response_model=QualityView,
    summary="Quality reports (gates, criteria scores, Q)",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_quality(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return manager.orchestrator.quality_view(run_id, owner_id=user.id)


@router.get(
    "/runs/{run_id}/article",
    response_model=ArticleView,
    summary="Assembled article",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_article(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    data = await manager.orchestrator.article_data(run_id, owner_id=user.id)
    return {"run_id": run_id, **data}


@router.get(
    "/runs/{run_id}/article/sections/{section_id}/history",
    response_model=SectionHistoryView,
    summary="Version history of one section",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_section_history(
    run_id: str, section_id: str, request: Request, user: CurrentUserDep
) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return await manager.orchestrator.section_history(run_id, section_id, owner_id=user.id)


@router.get(
    "/runs/{run_id}/article/sections/{section_id}/diff",
    response_model=SectionDiffView,
    summary="Diff between two versions of one section",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def get_section_diff(
    run_id: str,
    section_id: str,
    request: Request,
    user: CurrentUserDep,
    from_version: Annotated[int | None, Query(alias="from", ge=0)] = None,
    to_version: Annotated[int | None, Query(alias="to", ge=0)] = None,
) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return await manager.orchestrator.section_diff(
        run_id, section_id, from_version=from_version, to_version=to_version, owner_id=user.id
    )


@router.get(
    "/runs/{run_id}/article/snapshots",
    response_model=SnapshotList,
    summary="Whole-article snapshots",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def list_snapshots(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return await manager.orchestrator.snapshots_list(run_id, owner_id=user.id)


@router.post(
    "/runs/{run_id}/article/restore",
    response_model=RestoreView,
    summary="Roll the article back to a snapshot",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT, **_INVALID_BODY},
)
async def restore_article(
    run_id: str, payload: RestoreRequest, request: Request, user: CurrentUserDep
) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return await manager.orchestrator.restore_snapshot(
        run_id, payload.snapshot_id, dry_run=payload.dry_run, owner_id=user.id
    )


@router.get(
    "/runs/{run_id}/usage",
    response_model=RunUsageView,
    summary="Token usage",
    responses={**_UNAUTHORIZED, **_RUN_NOT_FOUND},
)
async def get_usage(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return manager.orchestrator.usage_summary(run_id, owner_id=user.id)
