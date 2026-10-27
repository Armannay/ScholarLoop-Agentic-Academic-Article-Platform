"""Run API: create, decide, stop, inspect (Step 9b, item 6).

Endpoints (all under the house ``/api`` prefix, all authenticated):

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
* ``GET /api/runs/{id}/digest`` - the bounded ``claude.md`` digest.
* ``GET /api/runs/{id}/ledger`` - append-only change entries.
* ``GET /api/runs/{id}/article`` - assembled markdown + sections + sources.
* ``GET /api/runs/{id}/usage`` - token usage by role/iteration + planner.

There is no Project table in CONTRACTS v1 (see the Step 9 handoff): a
``project_id`` is an opaque owner-scoped label stored on the run record.
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
from typing import Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator

from app.accounts.service import SqlAccountResolver
from app.agents.binding import UserBindingResolver
from app.agents.governor import CallBudgetGovernor
from app.agents.pool import PoolAgentProvider
from app.agents.registry import AgentRegistry
from app.agents.roles.factory import register_role_agents
from app.api.deps import CurrentUserDep, get_policies_dep, get_pool, get_rubric_dep
from app.contracts.brief import Brief
from app.contracts.common import Contract, Ulid
from app.contracts.ledger import ChangeEntry
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.verdict import Verdict
from app.orchestrator.intake import QuickEditRequest
from app.orchestrator.persistence import FileRunStore, RunRecord
from app.orchestrator.runner import Orchestrator, PlannerServiceAdapter, RunManager
from app.orchestrator.state_machine import RUNNING_STATES, TERMINAL_STATES, RunEvent, RunStateName
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
class RunCreate(Contract):
    """POST /projects/{id}/runs. Exactly one of ``brief`` / ``quick_edit``."""

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
    kind: str
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
    checkpoint_kind: str | None = None
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
            kind=record.kind.value,
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
            checkpoint_kind=(
                None if record.checkpoint_kind is None else record.checkpoint_kind.value
            ),
            on_complete=record.on_complete,
            user_stop_requested=record.user_stop_requested,
            running=running,
            tokens_spent=record.total_tokens_spent(),
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


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


# --------------------------------------------------------------------------- endpoints
@router.post(
    "/projects/{project_id}/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create and start a run",
)
async def create_run(
    project_id: str, payload: RunCreate, request: Request, user: CurrentUserDep
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


@router.post("/runs/{run_id}/decision", response_model=RunView, summary="Answer a checkpoint")
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


@router.post("/runs/{run_id}/stop", response_model=RunView, summary="Stop a run")
async def stop_run(run_id: str, request: Request, user: CurrentUserDep) -> RunView:
    manager = _manager_for(request, user.id)
    record = await manager.orchestrator.request_stop(run_id, owner_id=user.id)
    return _view(manager, record)


@router.get("/runs/{run_id}", response_model=RunView, summary="Run state")
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


@router.get("/runs/{run_id}/events", summary="Run events (SSE)")
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


@router.get("/runs/{run_id}/digest", response_model=DigestView, summary="Bounded digest")
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


@router.get("/runs/{run_id}/ledger", response_model=LedgerView, summary="Change ledger")
async def get_ledger(run_id: str, request: Request, user: CurrentUserDep) -> LedgerView:
    manager = _manager_for(request, user.id)
    record = manager.orchestrator.get_record(run_id, owner_id=user.id)
    return LedgerView(run_id=run_id, entries=list(record.entries))


@router.get("/runs/{run_id}/article", summary="Assembled article")
async def get_article(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    data = await manager.orchestrator.article_data(run_id, owner_id=user.id)
    return {"run_id": run_id, **data}


@router.get("/runs/{run_id}/usage", summary="Token usage")
async def get_usage(run_id: str, request: Request, user: CurrentUserDep) -> dict[str, Any]:
    manager = _manager_for(request, user.id)
    return manager.orchestrator.usage_summary(run_id, owner_id=user.id)
