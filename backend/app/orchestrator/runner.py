"""The orchestrator: the deterministic loop that owns every run.

Step 9b (items 1-3, 5-6 wiring) on top of Step 9a's pure controller. The
orchestrator is *not* an LLM and never writes article text. It:

1. validates intake deterministically (:mod:`app.orchestrator.intake`), or
   refuses with the exact missing fields;
2. asks the Planner for a plan (initial) or a plan delta (replanning) via
   the :class:`PlannerPort` seam - the real
   :class:`~app.planner.service.PlannerService` is adapted by
   :class:`PlannerServiceAdapter`, tests drive scripted doubles;
3. executes the plan through the scheduler (dependencies, ``max_parallel``,
   per-section locks, retries, timeouts, call-time binding resolution);
4. applies patches through the article store, appends ledger entries and
   source cards through the memory store, and **persists the whole run
   record after every task** (crash resume; completed tasks are idempotent
   by id and never re-run);
5. snapshots the article whenever a new best Q is measured;
6. hands the history to the pure loop controller and obeys its decision:
   stop (with a :class:`~app.orchestrator.stop.StopReport`), pause in
   ``CHECKPOINT`` for the user, or replan and iterate.

Termination is structural, not hopeful: every cycle increments ``iter`` and
the controller's iteration cap fires on count alone, so even a run in which
every planner/agent call misbehaves ends by ``max_iterations`` (or earlier,
by the error-streak stop).

Planner calls are serialised per orchestrator (the Step 8 service keeps
per-run brief/plan state), and planner token usage is attributed by diffing
the service's cumulative ``usage`` around each call.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final, Protocol, runtime_checkable

from app.agents.context import AgentRunState, RunState
from app.article.assemble import assemble, section_from
from app.article.patch import PatchRejected, StalePatch, diff_texts
from app.article.store import (
    ArticleStore,
    InMemoryStorage,
    Snapshot,
    SnapshotSection,
    StoredSection,
    StoredVersion,
)
from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, ChangeStatus, LoopAction, VerificationStatus
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import EMPTY_SECTION_HASH
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.clock import utc_now
from app.core.errors import NotFoundError, ScholarLoopError
from app.core.hashing import content_hash
from app.core.ids import new_ulid
from app.core.logging import get_logger
from app.core.policies import DEFAULT_POLICIES, Policies
from app.core.redaction import redact
from app.core.rubric import Rubric, resolve_sections
from app.memory.digest import DigestSnapshot, MemoryStore
from app.memory.errors import DuplicateEntryError
from app.memory.ledger import InMemoryLedger
from app.memory.sources import InMemorySourceStore
from app.orchestrator.errors import IntakeNeedsInput, OrchestratorError, RunConflict, RunNotFound
from app.orchestrator.intake import (
    IntakeKind,
    build_quick_edit_task,
    validate_payload,
)
from app.orchestrator.loop_controller import (
    IterationRecord,
    LoopController,
    LoopHistory,
    next_strikes,
)
from app.orchestrator.persistence import (
    ArticleDump,
    InMemoryRunStore,
    RunRecord,
    RunStore,
    RunStoreError,
    SectionDump,
    SnapshotDump,
    SnapshotSectionDump,
    VersionDump,
)
from app.orchestrator.scheduler import (
    AgentExecutor,
    SchedulerConfig,
    TaskOutcome,
    TaskScheduler,
    TaskStatus,
)
from app.orchestrator.state_machine import (
    RUNNING_STATES,
    TERMINAL_STATES,
    CheckpointKind,
    RunEvent,
    RunStateName,
    StateMachine,
    completed_task_ids,
)
from app.orchestrator.stop import (
    OnComplete,
    StopOption,
    UserDecision,
    build_stop_report,
)
from app.planner.budget import PlanBudget
from app.planner.probes import ArticleView, build_probe_report
from app.planner.service import PlannerService

__all__ = [
    "Orchestrator",
    "OrchestratorConfig",
    "PlannerPort",
    "PlannerServiceAdapter",
    "RunManager",
]

logger = get_logger(__name__)

_ABSTRACT_SECTION: Final[tuple[str, str]] = ("abstract", "Abstract")
_STOP_STATE: Final[dict[LoopAction, RunStateName]] = {
    LoopAction.STOP_EROSION: RunStateName.STOPPED_EROSION,
    LoopAction.STOP_BUDGET: RunStateName.STOPPED_BUDGET,
    LoopAction.STOP_ERRORS: RunStateName.STOPPED_ERRORS,
    LoopAction.STOP_USER: RunStateName.STOPPED_USER,
}


def _zero_usage() -> TokenUsage:
    return TokenUsage(in_=0, out=0, cached=0)


def _usage_delta(before: TokenUsage, after: TokenUsage) -> TokenUsage:
    return TokenUsage(
        in_=max(0, after.in_ - before.in_),
        out=max(0, after.out - before.out),
        cached=max(0, after.cached - before.cached),
    )


def _usage_add(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        in_=left.in_ + right.in_, out=left.out + right.out, cached=left.cached + right.cached
    )


# --------------------------------------------------------------------------- planner port
@runtime_checkable
class PlannerPort(Protocol):
    """The planner as the orchestrator needs it: plan, replan, evaluate.

    ``replan`` returns the *merged* plan (the adapter applies the Step 8
    delta). ``usage`` is the cumulative planner spend, diffed per call.
    """

    @property
    def usage(self) -> TokenUsage:
        """Cumulative tokens spent through this planner."""
        ...

    async def plan(self, *, brief: Brief, digest: str, budget: PlanBudget) -> Plan:
        """Produce the first plan for ``brief``."""
        ...

    async def replan(
        self,
        *,
        brief: Brief,
        current: Plan,
        digest: str,
        report: QualityReport | None,
        decision: LoopDecision | None,
        strategy_change: bool,
        budget: PlanBudget,
    ) -> Plan:
        """Return the merged next plan."""
        ...

    async def evaluate(
        self,
        *,
        brief: Brief,
        digest: str,
        report: QualityReport | None,
        view: ArticleView,
        iteration: int,
    ) -> Verdict:
        """Judge the article state (completion checkpoint, Step 9 item 5)."""
        ...


class PlannerServiceAdapter:
    """Adapt the Step 8 ``PlannerService`` to :class:`PlannerPort`.

    The service keeps the brief/plan it is working on internally; the
    adapter re-binds the brief before every replan/evaluate so one service
    can serve several interleaved runs of the same owner safely (the
    orchestrator additionally serialises planner calls).
    """

    def __init__(self, service: PlannerService, rubric: Rubric) -> None:
        self._service = service
        self._rubric = rubric

    @property
    def usage(self) -> TokenUsage:
        return self._service.usage

    async def plan(self, *, brief: Brief, digest: str, budget: PlanBudget) -> Plan:
        return await self._service.plan(brief, digest, budget)

    async def replan(
        self,
        *,
        brief: Brief,
        current: Plan,
        digest: str,
        report: QualityReport | None,
        decision: LoopDecision | None,
        strategy_change: bool,
        budget: PlanBudget,
    ) -> Plan:
        self._service.set_brief(brief)
        delta = await self._service.replan(
            digest, report, decision, strategy_change, plan=current, budget=budget
        )
        return delta.apply_to(current)

    async def evaluate(
        self,
        *,
        brief: Brief,
        digest: str,
        report: QualityReport | None,
        view: ArticleView,
        iteration: int,
    ) -> Verdict:
        self._service.set_brief(brief)
        probes = build_probe_report(view, self._rubric, report=report)
        return await self._service.evaluate(digest, report, probes, iteration=iteration)


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class OrchestratorConfig:
    """Execution knobs that are not loop policy (those live in policies.yaml)."""

    task_timeout_s: float = 180.0
    task_retries: int = 2
    backoff_base_s: float = 0.5
    backoff_factor: float = 2.0
    planner_backoff_s: float = 0.25

    def __post_init__(self) -> None:
        if self.task_timeout_s <= 0:
            raise ValueError("task_timeout_s must be positive")
        if self.task_retries < 0:
            raise ValueError("task_retries must be >= 0")
        if self.planner_backoff_s < 0:
            raise ValueError("planner_backoff_s must be >= 0")


@dataclass
class _Runtime:
    """Live per-run stores, rehydrated from (and dumped back to) the record."""

    article: ArticleStore
    memory: MemoryStore


# --------------------------------------------------------------------------- orchestrator
class Orchestrator:
    """Owns runs end-to-end: intake, loop, checkpoints, stops, resume."""

    def __init__(
        self,
        planner: PlannerPort,
        executor: AgentExecutor,
        *,
        policies: Policies = DEFAULT_POLICIES,
        rubric: Rubric | None = None,
        store: RunStore | None = None,
        config: OrchestratorConfig | None = None,
        binding_check: Callable[[Task], Any] | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = new_ulid,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._planner = planner
        self._policies = policies
        self._rubric = rubric
        self._store: RunStore = store if store is not None else InMemoryRunStore()
        self._config = config or OrchestratorConfig()
        self._clock = clock
        self._id_factory = id_factory
        self._sleep = sleep
        self._machine = StateMachine(self._store, clock=clock)
        self._controller = LoopController(policies)
        self._scheduler = TaskScheduler(
            executor,
            config=SchedulerConfig(
                max_parallel=policies.max_parallel,
                retries=self._config.task_retries,
                backoff_base_s=self._config.backoff_base_s,
                backoff_factor=self._config.backoff_factor,
                task_timeout_s=self._config.task_timeout_s,
            ),
            binding_check=binding_check,
            sleep=sleep,
        )
        self._runtimes: dict[str, _Runtime] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._planner_lock = asyncio.Lock()
        # Serialises the one-project-one-live-run check with the record's first
        # persist, so two concurrent create_run calls cannot both win.
        self._create_lock = threading.Lock()

    # -- introspection ------------------------------------------------------
    @property
    def store(self) -> RunStore:
        """The run-record store (persistence port)."""
        return self._store

    @property
    def policies(self) -> Policies:
        """The policies this orchestrator enforces."""
        return self._policies

    def is_running(self, run_id: str) -> bool:
        """True while ``run()`` for this id holds its lock in this process."""
        lock = self._run_locks.get(run_id)
        return lock is not None and lock.locked()

    def get_record(self, run_id: str, *, owner_id: str | None = None) -> RunRecord:
        """Load a run; another owner's run reads as not-found (no existence leak)."""
        record = self._store.load(run_id)
        if owner_id is not None and record.owner_id is not None and record.owner_id != owner_id:
            raise RunNotFound(f"no run '{run_id}'", run_id=run_id)
        return record

    def events_after(self, run_id: str, *, after_seq: int = -1) -> list[RunEvent]:
        """Persisted events with ``seq > after_seq`` (SSE replay/poll source)."""
        record = self._store.load(run_id)
        return [event for event in record.events if event.seq > after_seq]

    def project_runs(
        self, project_id: str, *, owner_id: str | None = None, limit: int = 50
    ) -> tuple[list[RunRecord], int]:
        """Newest-first run records of one (owner, project), plus the unpaged total.

        The read model behind ``GET /api/projects/{project_id}/runs``. It answers
        from persisted records only, so listing can never start, revive or mutate a
        run. ``total`` counts every matching record rather than the page, so the
        console can render "3 of 12" without a second request.

        Ordering is ``created_at`` descending, with the id as a deterministic
        tie-break (ULIDs are time-ordered, so equal timestamps stay stable).
        ``owner_id=None`` is the internal "every owner" call; the API always passes
        the caller, so a foreign project's runs are simply not there - not hidden
        behind a 403.
        """
        matching = [
            record
            for record in self._readable_records()
            if record.project_id == project_id and (owner_id is None or record.owner_id == owner_id)
        ]
        matching.sort(key=lambda record: (record.created_at, record.id), reverse=True)
        return matching[: max(limit, 0)], len(matching)

    def _readable_records(self) -> list[RunRecord]:
        """Every readable record in the store.

        One corrupt file must not blind every other read (a half-written record
        from a killed process is a fact of life, not a reason to fail the list),
        so it is skipped with a warning - exactly like the one-live-run guard.
        """
        records: list[RunRecord] = []
        for run_id in self._store.list_ids():
            try:
                record = self._store.get(run_id)
            except RunStoreError:
                logger.warning(
                    "skipping unreadable run record during a store scan",
                    extra={"run_id": run_id},
                )
                continue
            if record is not None:
                records.append(record)
        return records

    # -- creation -------------------------------------------------------------
    def _live_run_for(self, project_id: str, owner_id: str | None) -> RunRecord | None:
        """The live run of this (owner, project) pair, if one exists.

        Live means "still able to do work against the article": a running
        state, or ``CHECKPOINT`` (paused for a user decision, resumable).
        ``STOPPED_*`` and ``DONE`` are not live - they ended, and reviving
        one is an explicit decision, never a fresh run.
        """
        for run_id in self._store.list_ids():
            try:
                record = self._store.get(run_id)
            except RunStoreError:
                # One corrupt record must not block every other project's
                # intake; it cannot execute either, so it is not live.
                logger.warning(
                    "skipping unreadable run record during guard scan",
                    extra={"run_id": run_id},
                )
                continue
            if record is None:
                continue
            if record.project_id != project_id or record.owner_id != owner_id:
                continue
            if record.state in RUNNING_STATES or record.state is RunStateName.CHECKPOINT:
                return record
        return None

    def create_run(
        self,
        project_id: str,
        payload: Brief | Mapping[str, Any],
        *,
        owner_id: str | None = None,
        on_complete: OnComplete = OnComplete.ASK,
        max_wall_seconds: float | None = None,
    ) -> RunRecord:
        """Intake-validate and persist a new run in ``INTAKE``.

        One project, one live run (the OpenAPI ``active_run`` contract): a
        project whose run is still running or paused at a checkpoint refuses
        a second run with ``run_conflict`` / ``context.reason=active_run``,
        so a double-clicked button never puts two agents on the same article.

        Raises:
            IntakeNeedsInput: the payload is missing something only the user
                can supply; ``exc.result.missing_fields`` names the fields.
            RunConflict: the owner-scope project already has a live run.
        """
        result = validate_payload(payload, rubric=self._rubric)
        if not result.ok:
            raise IntakeNeedsInput(result)
        with self._create_lock:
            live = self._live_run_for(project_id, owner_id)
            if live is not None:
                raise RunConflict(
                    f"project '{project_id}' already has a running run '{live.id}'; "
                    "stop it or wait for the checkpoint",
                    reason="active_run",
                    project_id=project_id,
                    active_run_id=live.id,
                    state=live.state.value,
                )
            now = self._clock()
            record = RunRecord(
                id=self._id_factory(),
                project_id=project_id,
                owner_id=owner_id,
                kind=result.kind,
                brief=result.brief,
                on_complete=on_complete,
                max_wall_seconds=max_wall_seconds,
                created_at=now,
                updated_at=now,
            )
            if result.kind is IntakeKind.QUICK_EDIT:
                assert result.quick_edit is not None
                task = build_quick_edit_task(result.quick_edit, id_factory=self._id_factory)
                record.quick_edit_task = task
                record.plan = Plan(version=1, goal="Quick edit (planner-free)", tasks=[task])
            self._machine.emit(
                record,
                "run_created",
                detail={"kind": result.kind.value, "project_id": project_id},
            )
        return record

    # -- runtime (de)hydration --------------------------------------------------
    def _runtime_for(self, record: RunRecord) -> _Runtime:
        cached = self._runtimes.get(record.id)
        if cached is not None:
            return cached
        storage = InMemoryStorage()
        for section in record.article.sections:
            state = section.state
            storage.write_section(
                StoredSection(
                    id=state.id,
                    title=state.title,
                    order=state.order,
                    version=state.version,
                    hash=state.hash,
                    words=state.words,
                    citations=tuple(state.citations),
                    status=state.status,
                    open_issues=tuple(state.open_issues),
                    text=section.text,
                    updated_at=section.updated_at,
                )
            )
        for version in record.article.versions:
            storage.append_version(
                StoredVersion(
                    section_id=version.section_id,
                    version=version.version,
                    hash=version.hash,
                    words=version.words,
                    text=version.text,
                    status=version.status,
                    patch_id=version.patch_id,
                    entry_id=version.entry_id,
                    chars_added=version.chars_added,
                    chars_removed=version.chars_removed,
                    created_at=version.created_at,
                    q_after=version.q_after,
                )
            )
        for snapshot in record.article.snapshots:
            storage.write_snapshot(
                Snapshot(
                    id=snapshot.id,
                    label=snapshot.label,
                    q=snapshot.q,
                    iter=snapshot.iter,
                    created_at=snapshot.created_at,
                    sections=tuple(
                        SnapshotSection(state=part.state, text=part.text)
                        for part in snapshot.sections
                    ),
                )
            )
        article = ArticleStore(storage, clock=self._clock, id_factory=self._id_factory)
        memory = MemoryStore(
            self._policies,
            brief=record.brief,
            plan=record.plan.model_copy(deep=True) if record.plan is not None else None,
            ledger=InMemoryLedger(entries=record.entries),
            sources=InMemorySourceStore(cards=list(record.sources), role=AgentRole.VALIDATOR),
        )
        for report in record.reports:
            memory.record_report(report)
        memory.update_sections([section.state for section in record.article.sections])
        runtime = _Runtime(article=article, memory=memory)
        self._runtimes[record.id] = runtime
        return runtime

    async def _sync(self, record: RunRecord, runtime: _Runtime) -> None:
        """Dump the live stores back into the record (not yet saved)."""
        storage = runtime.article.storage
        rows = storage.read_sections()
        versions: list[VersionDump] = []
        for row in rows:
            for version in storage.read_versions(row.id):
                versions.append(
                    VersionDump(
                        section_id=version.section_id,
                        version=version.version,
                        hash=version.hash,
                        words=version.words,
                        text=version.text,
                        status=version.status,
                        patch_id=version.patch_id,
                        entry_id=version.entry_id,
                        chars_added=version.chars_added,
                        chars_removed=version.chars_removed,
                        created_at=version.created_at,
                        q_after=version.q_after,
                    )
                )
        snapshots = await runtime.article.snapshots()
        record.article = ArticleDump(
            sections=[
                SectionDump(state=row.state(), text=row.text, updated_at=row.updated_at)
                for row in rows
            ],
            versions=versions,
            snapshots=[
                SnapshotDump(
                    id=snapshot.id,
                    label=snapshot.label,
                    q=snapshot.q,
                    iter=snapshot.iter,
                    created_at=snapshot.created_at,
                    sections=[
                        SnapshotSectionDump(state=part.state, text=part.text)
                        for part in snapshot.sections
                    ],
                )
                for snapshot in snapshots
            ],
        )
        record.entries = runtime.memory.ledger.entries()
        record.reports = runtime.memory.reports
        record.sources = runtime.memory.sources.list_sources()
        plan = runtime.memory.plan
        if plan is not None:
            record.plan = plan.model_copy(deep=True)
        runtime.memory.update_sections([row.state() for row in rows])

    def _merge_external_flags(self, record: RunRecord) -> None:
        """Pick up a stop requested through another record copy (the API)."""
        stored = self._store.get(record.id)
        if stored is not None and stored.user_stop_requested:
            record.user_stop_requested = True

    async def _persist(self, record: RunRecord, runtime: _Runtime) -> None:
        self._merge_external_flags(record)
        await self._sync(record, runtime)
        self._machine.persist(record)

    async def _transition(
        self,
        record: RunRecord,
        runtime: _Runtime,
        target: RunStateName,
        *,
        event_type: str = "state_changed",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._merge_external_flags(record)
        await self._sync(record, runtime)
        self._machine.transition(record, target, event_type=event_type, detail=detail)

    # -- sections ---------------------------------------------------------------
    async def _ensure_section(self, runtime: _Runtime, section_id: str) -> None:
        if runtime.article.storage.read_section(section_id) is not None:
            return
        title = section_id.replace("-", " ").replace("_", " ").title() or section_id
        await runtime.article.create_section(section_id, title)

    async def _seed_sections(self, record: RunRecord, runtime: _Runtime) -> None:
        """Create the rubric's sections (plus abstract) on first entry."""
        if runtime.article.section_ids() or record.brief is None or self._rubric is None:
            return
        order = 0
        await runtime.article.create_section(
            _ABSTRACT_SECTION[0], _ABSTRACT_SECTION[1], order=order
        )
        for spec in resolve_sections(self._rubric, record.brief.article_type):
            order += 1
            await runtime.article.create_section(spec.id, spec.title, order=order)

    async def _ensure_task_sections(self, runtime: _Runtime, plan: Plan) -> None:
        for task in plan.tasks:
            for section_id in task.section_ids:
                await self._ensure_section(runtime, section_id)

    # -- planner helpers ----------------------------------------------------------
    def _plan_budget(self, record: RunRecord, *, iteration: int) -> PlanBudget:
        cap = self._policies.run_token_cap + record.extra_token_budget
        remaining = max(1, cap - record.total_tokens_spent())
        return PlanBudget(remaining_run_tokens=remaining, iteration=iteration)

    async def _digest_text(self, record: RunRecord, runtime: _Runtime) -> str:
        runtime.memory.update_sections(await runtime.article.list_state())
        if record.plan is not None:
            runtime.memory.set_plan(record.plan)
        text = runtime.memory.build_digest().markdown
        extras: list[str] = []
        if record.goal_override:
            extras.append(f"User goal update: {record.goal_override}")
        extras.extend(f"User input: {note}" for note in record.user_notes[-3:])
        if extras:
            # User-supplied text is data for the Planner, quoted after the
            # bounded digest; the planner's context builder trims to budget.
            text = f"{text}\n\n" + "\n".join(extras)
        return text

    async def _planner_call(
        self,
        record: RunRecord,
        runtime: _Runtime,
        *,
        kind: str,
        call: Callable[..., Awaitable[Any]],
        kwargs: dict[str, Any],
    ) -> Any | None:
        """One planner operation with bounded retries; ``None`` = stopped on errors.

        Provider/output failures retry up to ``policies.error_streak``
        attempts (with backoff); the final failure stops the run with
        ``STOP_ERRORS`` rather than looping on a dead planner.
        """
        attempts = max(1, self._policies.error_streak)
        for attempt in range(attempts):
            before = self._planner.usage
            try:
                async with self._planner_lock:
                    result = await call(**kwargs)
            except Exception as exc:  # planner/provider failures are run errors here
                record.planner_tokens = _usage_add(
                    record.planner_tokens, _usage_delta(before, self._planner.usage)
                )
                record.last_error = redact(f"{kind} failed: {exc}")[:400]
                self._machine.emit(
                    record,
                    "planner_failed",
                    detail={"kind": kind, "attempt": attempt + 1},
                    save=False,
                )
                await self._persist(record, runtime)
                logger.warning("planner %s failed (attempt %s)", kind, attempt + 1)
                if attempt + 1 >= attempts:
                    decision = LoopDecision(
                        iter=record.iter,
                        action=LoopAction.STOP_ERRORS,
                        reasons=["planner_error_streak"],
                        evidence={"kind": kind, "attempts": attempts},
                    )
                    record.decisions.append(decision)
                    await self._finalize_stop(record, runtime, decision)
                    return None
                if self._config.planner_backoff_s > 0:
                    await self._sleep(self._config.planner_backoff_s * (attempt + 1))
                continue
            record.planner_tokens = _usage_add(
                record.planner_tokens, _usage_delta(before, self._planner.usage)
            )
            record.last_error = None
            return result
        return None  # pragma: no cover - the loop always returns

    # -- applying agent results ---------------------------------------------------
    def _apply_callback(
        self, record: RunRecord, runtime: _Runtime
    ) -> Callable[[Task, AgentResult], Awaitable[AgentResult | None]]:
        async def apply_result(task: Task, result: AgentResult) -> AgentResult | None:
            return await self._apply_result(record, runtime, task, result)

        return apply_result

    async def _apply_result(
        self, record: RunRecord, runtime: _Runtime, task: Task, result: AgentResult
    ) -> AgentResult | None:
        """Apply patches, ingest sources, append the entry, persist. Per task.

        A rejected patch or an illegal source card flips the entry to
        ``failed`` (the adjusted result is returned so the scheduler does
        not count the task complete): the ledger must never claim ``ok``
        for work that did not land.
        """
        memory = runtime.memory
        failure: str | None = None

        for patch in result.patches:
            await self._ensure_section(runtime, patch.section_id)
            try:
                await runtime.article.apply_patch(patch, entry_id=result.entry.id)
            except (StalePatch, PatchRejected, ScholarLoopError) as exc:
                failure = f"patch_rejected:{patch.section_id}"
                self._machine.emit(
                    record,
                    "patch_rejected",
                    detail={
                        "task_id": task.id,
                        "section_id": patch.section_id,
                        "error": redact(str(exc))[:200],
                    },
                    save=False,
                )
                break

        if failure is None and result.source_cards:
            try:
                memory.sources.ingest_many(result.source_cards, role=result.entry.agent_role)
            except ScholarLoopError as exc:
                failure = "source_card_rejected"
                self._machine.emit(
                    record,
                    "source_card_rejected",
                    detail={"task_id": task.id, "error": redact(str(exc))[:200]},
                    save=False,
                )

        adjusted: AgentResult | None = None
        entry = result.entry
        if failure is not None:
            entry = entry.model_copy(update={"status": ChangeStatus.FAILED})
            adjusted = AgentResult(
                entry=entry,
                patches=[],
                source_cards=result.source_cards if failure != "source_card_rejected" else [],
                sections=[],
                report=None,
            )

        # Section status/issue updates (reviewer/validator) - best effort,
        # never a reason to fail the task that produced them.
        if failure is None:
            for section in result.sections:
                row = runtime.article.storage.read_section(section.id)
                if row is None:
                    continue
                try:
                    if list(section.open_issues) != list(row.open_issues):
                        await runtime.article.set_open_issues(section.id, list(section.open_issues))
                    if (
                        section.status is not row.status
                        and section.status.value != "empty"
                        and row.version > 0
                    ):
                        await runtime.article.set_status(section.id, section.status)
                except ScholarLoopError:
                    self._machine.emit(
                        record,
                        "section_update_rejected",
                        detail={"task_id": task.id, "section_id": section.id},
                        save=False,
                    )

        # Idempotent replay after a crash between apply and persist.
        with contextlib.suppress(DuplicateEntryError):
            memory.ledger.append(entry)
        if failure is None and result.report is not None:
            memory.record_report(result.report)

        self._machine.emit(
            record,
            "task_failed" if entry.status is ChangeStatus.FAILED else "task_completed",
            detail={"task_id": task.id, "role": task.role.value, "status": entry.status.value},
            save=False,
        )
        await self._persist(record, runtime)
        return adjusted

    async def _record_failed_outcomes(
        self, record: RunRecord, runtime: _Runtime, outcomes: Mapping[str, TaskOutcome], plan: Plan
    ) -> None:
        """One synthetic ``failed`` entry per exhausted task (error accounting)."""
        changed = False
        for outcome in outcomes.values():
            if outcome.status != TaskStatus.FAILED:
                continue
            task = plan.task_by_id(outcome.task_id)
            if task is None:
                continue
            code = outcome.error_code or "task_failed"
            # "No usable account" is a user condition, not an agent failure:
            # the entry says needs_user_input so the controller pauses with
            # ASK_USER instead of burning the error-streak budget.
            status = (
                ChangeStatus.NEEDS_USER_INPUT
                if code == "no_account_available"
                else ChangeStatus.FAILED
            )
            summary = f"Task failed [{code}]: {task.title}"[:300]
            entry = ChangeEntry(
                id=self._id_factory(),
                iter=record.iter,
                task_id=task.id,
                role=task.role,
                targets=list(task.section_ids),
                summary=" ".join(summary.split()[:60]),
                tokens=_zero_usage(),
                status=status,
            )
            runtime.memory.ledger.append(entry)
            self._machine.emit(
                record,
                "task_failed",
                detail={
                    "task_id": task.id,
                    "role": task.role.value,
                    "error_code": code,
                    "attempts": outcome.attempts,
                },
                save=False,
            )
            changed = True
        for outcome in outcomes.values():
            if outcome.attempts:
                record.task_attempts[outcome.task_id] = (
                    record.task_attempts.get(outcome.task_id, 0) + outcome.attempts
                )
                changed = True
        if changed:
            await self._persist(record, runtime)

    # -- history / decisions ---------------------------------------------------------
    def _unverified_count(self, runtime: _Runtime) -> int:
        sources = {card.id: card for card in runtime.memory.sources.list_sources()}
        count = 0
        for row in runtime.article.storage.read_sections():
            for citation in row.citations:
                card = sources.get(citation.source_id or "")
                if card is None or card.verification is not VerificationStatus.VERIFIED:
                    count += 1
        return count

    def _history(self, record: RunRecord) -> LoopHistory:
        entries_by_iter: dict[int, list[ChangeEntry]] = {}
        for entry in record.entries:
            entries_by_iter.setdefault(entry.iter, []).append(entry)
        reports_by_iter: dict[int, QualityReport] = {}
        for report in record.reports:
            reports_by_iter[report.iter] = report
        records: list[IterationRecord] = []
        for iteration in range(1, record.iter + 1):
            entries = entries_by_iter.get(iteration, [])
            tokens = _zero_usage()
            for entry in entries:
                tokens = _usage_add(tokens, entry.tokens)
            records.append(
                IterationRecord(
                    iter=iteration,
                    report=reports_by_iter.get(iteration),
                    entries=entries,
                    tokens=tokens,
                    unverified_citations=record.unverified_by_iter.get(str(iteration), 0),
                )
            )
        return LoopHistory(
            records=records,
            user_stop_requested=record.user_stop_requested,
            tokens_spent=record.total_tokens_spent(),
            wall_seconds=record.wall_seconds,
            verdict=record.verdict if record.verdict_iter == record.iter else None,
            strikes=dict(record.strikes),
            max_iterations=self._policies.max_iterations + record.extra_iterations,
            token_cap=self._policies.run_token_cap + record.extra_token_budget,
            max_wall_seconds=record.max_wall_seconds,
        )

    def _accept_decision(
        self, record: RunRecord, history: LoopHistory, decision: LoopDecision
    ) -> None:
        record.decisions.append(decision)
        record.strikes = next_strikes(history, decision)
        self._machine.emit(
            record,
            "loop_decision",
            detail={"action": decision.action.value, "reasons": list(decision.reasons)},
            save=False,
        )

    async def _finalize_stop(
        self, record: RunRecord, runtime: _Runtime, decision: LoopDecision
    ) -> None:
        """Snapshot-aware terminal transition with a StopReport."""
        if record.best_snapshot_id is None:
            best = await runtime.article.best_snapshot()
            if best is not None:
                record.best_snapshot_id = best.id
                record.best_q = best.q
        history = self._history(record)
        record.stop_report = build_stop_report(
            run_id=record.id,
            decision=decision,
            history=history,
            best_snapshot_id=record.best_snapshot_id,
            verdict=record.verdict if record.verdict_iter == record.iter else None,
            created_at=self._clock(),
        )
        target = _STOP_STATE.get(decision.action, RunStateName.DONE)
        record.checkpoint_kind = None
        await self._transition(
            record,
            runtime,
            target,
            event_type="run_stopped",
            detail={"action": decision.action.value, "reasons": list(decision.reasons)},
        )

    async def _checkpoint(
        self,
        record: RunRecord,
        runtime: _Runtime,
        *,
        kind: CheckpointKind,
        decision: LoopDecision,
    ) -> None:
        record.checkpoint_kind = kind
        record.stop_report = build_stop_report(
            run_id=record.id,
            decision=decision,
            history=self._history(record),
            best_snapshot_id=record.best_snapshot_id,
            verdict=record.verdict if record.verdict_iter == record.iter else None,
            created_at=self._clock(),
        )
        await self._transition(
            record,
            runtime,
            RunStateName.CHECKPOINT,
            event_type="checkpoint_reached",
            detail={"kind": kind.value, "action": decision.action.value},
        )

    # -- run-state factory -------------------------------------------------------------
    def _run_state_factory(
        self, record: RunRecord, runtime: _Runtime
    ) -> Callable[[], Awaitable[RunState]]:
        async def factory() -> RunState:
            return AgentRunState(
                iter=record.iter,
                brief=record.brief,
                sections=tuple(await runtime.article.list_state()),
                sources=tuple(runtime.memory.sources.list_sources()),
            )

        return factory

    # -- main entry ---------------------------------------------------------------------
    async def run(self, run_id: str) -> RunRecord:
        """Drive a run to its next terminal or checkpoint state. Idempotent.

        Safe to call after a crash (record rehydrated, completed tasks
        skipped), on a terminal run (returned unchanged) or on a checkpoint
        (returned unchanged - only a user decision moves it).
        """
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            record = self._store.load(run_id)
            if record.state in TERMINAL_STATES or record.state is RunStateName.CHECKPOINT:
                return record
            runtime = self._runtime_for(record)
            started = time.monotonic()
            try:
                if record.kind is IntakeKind.QUICK_EDIT:
                    await self._run_quick_edit(record, runtime)
                else:
                    await self._run_loop(record, runtime)
            finally:
                record.wall_seconds += max(0.0, time.monotonic() - started)
                await self._persist(record, runtime)
            return record

    async def _run_quick_edit(self, record: RunRecord, runtime: _Runtime) -> None:
        task = record.quick_edit_task
        if task is None or record.plan is None:
            raise OrchestratorError("quick-edit run without its task", run_id=record.id)
        if record.iter == 0:
            record.iter = 1
        await self._seed_sections(record, runtime)
        await self._ensure_section(runtime, task.section_ids[0])
        if record.state is not RunStateName.EXECUTING:
            await self._transition(record, runtime, RunStateName.EXECUTING)
        completed = completed_task_ids(runtime.memory.ledger.entries())
        if task.id in completed:
            await self._transition(record, runtime, RunStateName.DONE)
            return
        batch = await self._scheduler.run(
            [task],
            run_state_factory=self._run_state_factory(record, runtime),
            completed_ids=completed,
            apply_result=self._apply_callback(record, runtime),
            should_stop=lambda: self._external_stop(record),
        )
        await self._record_failed_outcomes(record, runtime, batch.outcomes, record.plan)
        outcome = batch.outcomes.get(task.id)
        ledger_needs_input = any(
            entry.task_id == task.id for entry in runtime.memory.ledger.needs_user_input_entries()
        )
        if batch.any_needs_user_input or ledger_needs_input:
            decision = LoopDecision(
                iter=record.iter,
                action=LoopAction.ASK_USER,
                reasons=["needs_user_input"],
                evidence={"task_id": task.id},
            )
            record.decisions.append(decision)
            await self._checkpoint(
                record, runtime, kind=CheckpointKind.NEEDS_INPUT, decision=decision
            )
        elif outcome is not None and outcome.counts_as_completed:
            await self._transition(record, runtime, RunStateName.DONE, event_type="run_completed")
        elif self._external_stop(record):
            decision = LoopDecision(
                iter=record.iter,
                action=LoopAction.STOP_USER,
                reasons=["user_stop_requested"],
                evidence={"task_id": task.id},
            )
            record.decisions.append(decision)
            await self._finalize_stop(record, runtime, decision)
        else:
            decision = LoopDecision(
                iter=record.iter,
                action=LoopAction.STOP_ERRORS,
                reasons=["quick_edit_failed"],
                evidence={"task_id": task.id},
            )
            record.decisions.append(decision)
            await self._finalize_stop(record, runtime, decision)

    def _external_stop(self, record: RunRecord) -> bool:
        self._merge_external_flags(record)
        return record.user_stop_requested

    async def _run_loop(self, record: RunRecord, runtime: _Runtime) -> None:
        brief = record.brief
        if brief is None:
            raise OrchestratorError("brief run without a brief", run_id=record.id)
        await self._seed_sections(record, runtime)
        await self._persist(record, runtime)

        while True:
            self._merge_external_flags(record)
            if record.user_stop_requested:
                decision = self._controller.evaluate(self._history(record))
                self._accept_decision(record, self._history(record), decision)
                await self._finalize_stop(record, runtime, decision)
                return

            # -- planning / replanning -----------------------------------------
            if record.plan is None:
                if record.state is RunStateName.INTAKE:
                    await self._transition(record, runtime, RunStateName.PLANNING)
                digest = await self._digest_text(record, runtime)
                budget = self._plan_budget(record, iteration=record.iter + 1)
                plan = await self._planner_call(
                    record,
                    runtime,
                    kind="plan",
                    call=self._planner.plan,
                    kwargs={"brief": brief, "digest": digest, "budget": budget},
                )
                if plan is None:
                    return  # stopped on planner errors
                record.plan = plan
                runtime.memory.set_plan(plan)
                self._machine.emit(
                    record,
                    "plan_accepted",
                    detail={"version": plan.version, "tasks": len(plan.tasks)},
                    save=False,
                )
                await self._persist(record, runtime)
            elif record.state is RunStateName.REPLANNING:
                last = record.decisions[-1] if record.decisions else None
                strategy = bool(last and last.evidence.get("strategy_change") is True)
                digest = await self._digest_text(record, runtime)
                budget = self._plan_budget(record, iteration=record.iter + 1)
                current = record.plan
                report = record.reports[-1] if record.reports else None
                plan = await self._planner_call(
                    record,
                    runtime,
                    kind="replan",
                    call=self._planner.replan,
                    kwargs={
                        "brief": brief,
                        "current": current,
                        "digest": digest,
                        "report": report,
                        "decision": last,
                        "strategy_change": strategy,
                        "budget": budget,
                    },
                )
                if plan is None:
                    return
                record.plan = plan
                runtime.memory.set_plan(plan)
                self._machine.emit(
                    record,
                    "plan_accepted",
                    detail={
                        "version": plan.version,
                        "tasks": len(plan.tasks),
                        "strategy_change": strategy,
                    },
                    save=False,
                )
                await self._persist(record, runtime)

            plan = record.plan
            assert plan is not None

            # Crash-resume: a record persisted mid-EXECUTING/VALIDATING/
            # DECIDING re-enters at its phase. The iteration counter was
            # already incremented (and persisted) when the iteration
            # started, so it must not increment again; completed tasks are
            # skipped by id, so re-entering EXECUTING is safe.
            resume_phase = record.state

            # -- executing ---------------------------------------------------------
            if resume_phase not in (RunStateName.VALIDATING, RunStateName.DECIDING):
                if record.state is not RunStateName.EXECUTING:
                    await self._transition(record, runtime, RunStateName.EXECUTING)
                    record.iter += 1
                    self._machine.emit(
                        record, "iteration_started", detail={"iter": record.iter}, save=False
                    )
                await self._ensure_task_sections(runtime, plan)
                await self._persist(record, runtime)

                batch = await self._scheduler.run(
                    plan.tasks,
                    run_state_factory=self._run_state_factory(record, runtime),
                    completed_ids=completed_task_ids(runtime.memory.ledger.entries()),
                    apply_result=self._apply_callback(record, runtime),
                    should_stop=lambda: self._external_stop(record),
                )
                await self._record_failed_outcomes(record, runtime, batch.outcomes, plan)

            # -- validating ---------------------------------------------------------
            if record.state not in (RunStateName.DECIDING, RunStateName.VALIDATING):
                await self._transition(record, runtime, RunStateName.VALIDATING)
            record.unverified_by_iter[str(record.iter)] = self._unverified_count(runtime)
            report = next(
                (item for item in reversed(runtime.memory.reports) if item.iter == record.iter),
                None,
            )
            if report is not None:
                await runtime.article.record_quality(float(report.Q))
                if record.best_q is None or float(report.Q) > record.best_q:
                    snapshot = await runtime.article.snapshot(
                        f"best-iter-{record.iter}", q=float(report.Q), iter=record.iter
                    )
                    record.best_snapshot_id = snapshot.id
                    record.best_q = float(report.Q)
                self._machine.emit(
                    record,
                    "validation_completed",
                    detail={"Q": report.Q, "failed_gates": report.failed_gates},
                    save=False,
                )
            else:
                self._machine.emit(record, "validation_missing", detail={}, save=False)
            await self._persist(record, runtime)

            # -- deciding ------------------------------------------------------------
            await self._transition(record, runtime, RunStateName.DECIDING)
            history = self._history(record)
            decision = self._controller.evaluate(history)
            self._accept_decision(record, history, decision)

            if decision.action in _STOP_STATE:
                await self._finalize_stop(record, runtime, decision)
                return
            if decision.action is LoopAction.ASK_USER:
                await self._checkpoint(
                    record, runtime, kind=CheckpointKind.NEEDS_INPUT, decision=decision
                )
                return
            if decision.action is LoopAction.OFFER_FINISH:
                flow = await self._handle_offer_finish(record, runtime, decision)
                if flow == "return":
                    return
                continue  # replanning (verdict veto or continue_once)
            # CONTINUE
            await self._transition(record, runtime, RunStateName.REPLANNING)

    async def _handle_offer_finish(
        self, record: RunRecord, runtime: _Runtime, decision: LoopDecision
    ) -> str:
        """Completion candidate -> Planner verdict -> on_complete checkpoint logic."""
        brief = record.brief
        assert brief is not None
        current = decision
        if current.evidence.get("needs_planner_evaluation") is True:
            view = await self._article_view(record, runtime)
            digest = await self._digest_text(record, runtime)
            report = record.reports[-1] if record.reports else None
            verdict = await self._planner_call(
                record,
                runtime,
                kind="evaluate",
                call=self._planner.evaluate,
                kwargs={
                    "brief": brief,
                    "digest": digest,
                    "report": report,
                    "view": view,
                    "iteration": record.iter,
                },
            )
            if verdict is None:
                if record.state in TERMINAL_STATES:
                    return "return"  # planner error streak already stopped the run
                await self._checkpoint(
                    record, runtime, kind=CheckpointKind.EVALUATION_FAILED, decision=current
                )
                return "return"
            record.verdict = verdict
            record.verdict_iter = record.iter
            self._machine.emit(
                record,
                "planner_evaluated",
                detail={
                    "meets_minimum": verdict.meets_minimum,
                    "recommendation": verdict.recommendation.value,
                },
                save=False,
            )
            await self._persist(record, runtime)
            history = self._history(record)
            current = self._controller.evaluate(history)
            self._accept_decision(record, history, current)
            if current.action in _STOP_STATE:
                await self._finalize_stop(record, runtime, current)
                return "return"
            if current.action is not LoopAction.OFFER_FINISH:
                await self._transition(record, runtime, RunStateName.REPLANNING)
                return "continue"

        if record.on_complete is OnComplete.FINISH:
            record.stop_report = build_stop_report(
                run_id=record.id,
                decision=current,
                history=self._history(record),
                best_snapshot_id=record.best_snapshot_id,
                verdict=record.verdict,
                created_at=self._clock(),
            )
            await self._transition(record, runtime, RunStateName.DONE, event_type="run_completed")
            return "return"
        if record.on_complete is OnComplete.CONTINUE_ONCE and not record.continue_once_used:
            record.continue_once_used = True
            self._machine.emit(record, "continue_once_used", detail={}, save=False)
            await self._transition(record, runtime, RunStateName.REPLANNING)
            return "continue"
        await self._checkpoint(record, runtime, kind=CheckpointKind.COMPLETION, decision=current)
        return "return"

    async def _article_view(self, record: RunRecord, runtime: _Runtime) -> ArticleView:
        brief = record.brief
        assert brief is not None
        rows = runtime.article.storage.read_sections()
        rows.sort(key=lambda row: (row.order, row.id))
        return ArticleView.from_sections(
            brief,
            [row.state() for row in rows],
            texts={row.id: row.text for row in rows},
            sources=runtime.memory.sources.list_sources(),
            flagged_source_ids=[
                card.id
                for card in runtime.memory.sources.list_sources()
                if card.verification is VerificationStatus.MISMATCH
            ],
        )

    # -- user operations ------------------------------------------------------------
    async def request_stop(self, run_id: str, *, owner_id: str | None = None) -> RunRecord:
        """Ask a run to stop; finalise immediately when nothing is executing."""
        record = self.get_record(run_id, owner_id=owner_id)
        if record.state in TERMINAL_STATES:
            return record
        if self.is_running(run_id):
            record.user_stop_requested = True
            self._machine.emit(record, "stop_requested", detail={})
            return record
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            record = self.get_record(run_id, owner_id=owner_id)
            if record.state in TERMINAL_STATES:
                return record
            record.user_stop_requested = True
            runtime = self._runtime_for(record)
            history = self._history(record)
            decision = self._controller.evaluate(history)
            self._accept_decision(record, history, decision)
            await self._finalize_stop(record, runtime, decision)
            return record

    async def apply_decision(
        self, run_id: str, decision: UserDecision, *, owner_id: str | None = None
    ) -> RunRecord:
        """Apply a checkpoint/stop choice. Caller re-runs the run afterwards."""
        if self.is_running(run_id):
            raise RunConflict(
                "run is executing; stop it before deciding",
                run_id=run_id,
                reason="invalid_transition",
            )
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            record = self.get_record(run_id, owner_id=owner_id)
            if record.state is RunStateName.DONE:
                raise RunConflict(
                    "run is done; no decision applies",
                    run_id=run_id,
                    reason="invalid_transition",
                    state=record.state.value,
                )
            if record.state in RUNNING_STATES:
                raise RunConflict(
                    f"run is {record.state.value}; decisions apply at checkpoints/stops",
                    run_id=run_id,
                    reason="invalid_transition",
                    state=record.state.value,
                )
            runtime = self._runtime_for(record)
            option = decision.option
            detail = {"option": option.value}

            if option is StopOption.ROLLBACK:
                if record.best_snapshot_id is None:
                    raise RunConflict(
                        "no best snapshot to roll back to",
                        run_id=run_id,
                        reason="no_best_snapshot",
                    )
                await runtime.article.restore(record.best_snapshot_id)
                self._machine.emit(record, "rollback_applied", detail=detail, save=False)
                await self._transition(
                    record, runtime, RunStateName.DONE, event_type="run_completed"
                )
                return record

            if option is StopOption.FINISH:
                await self._transition(
                    record, runtime, RunStateName.DONE, event_type="run_completed"
                )
                return record

            if option is StopOption.ADD_BUDGET and not (
                decision.extra_tokens or decision.extra_iterations
            ):
                raise RunConflict(
                    "add_budget needs extra_tokens or extra_iterations",
                    run_id=run_id,
                    reason="missing_budget_extension",
                )
            if option is StopOption.EDIT_GOALS and not decision.new_goal:
                raise RunConflict("edit_goals needs new_goal", run_id=run_id, reason="missing_goal")

            if decision.extra_tokens:
                record.extra_token_budget += decision.extra_tokens
            if decision.extra_iterations:
                record.extra_iterations += decision.extra_iterations
            if decision.new_goal:
                record.goal_override = decision.new_goal
            if decision.note:
                record.user_notes = [*record.user_notes, decision.note][-20:]
            if option is StopOption.CONTINUE_ANYWAY:
                if not decision.extra_iterations:
                    record.extra_iterations += 1
                if record.state is RunStateName.STOPPED_BUDGET and not decision.extra_tokens:
                    record.extra_token_budget += self._policies.iteration_token_cap
            record.strikes = {}
            record.user_stop_requested = False
            record.checkpoint_kind = None
            record.last_error = None

            if record.kind is IntakeKind.QUICK_EDIT:
                task = record.quick_edit_task
                if task is not None and decision.note:
                    updated = task.model_copy(
                        update={"instructions": f"{task.instructions}\nUser input: {decision.note}"}
                    )
                    record.quick_edit_task = updated
                    record.plan = Plan(
                        version=(record.plan.version + 1) if record.plan else 1,
                        goal="Quick edit (planner-free)",
                        tasks=[updated],
                    )
                    runtime.memory.set_plan(record.plan)
                self._machine.emit(record, "user_decision", detail=detail, save=False)
                # REPLANNING is the universal "run again" state (legal from
                # CHECKPOINT and every STOPPED_*); the quick-edit path moves
                # REPLANNING -> EXECUTING when it re-enters.
                await self._transition(record, runtime, RunStateName.REPLANNING)
                return record

            self._machine.emit(record, "user_decision", detail=detail, save=False)
            await self._transition(record, runtime, RunStateName.REPLANNING)
            return record

    # -- read models for the API ------------------------------------------------------
    async def digest_snapshot(self, run_id: str, *, owner_id: str | None = None) -> DigestSnapshot:
        """The current bounded ``claude.md`` digest for a run."""
        record = self.get_record(run_id, owner_id=owner_id)
        runtime = self._runtime_for(record)
        runtime.memory.update_sections(await runtime.article.list_state())
        if record.plan is not None:
            runtime.memory.set_plan(record.plan)
        return runtime.memory.build_digest()

    async def article_data(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Assembled markdown + per-section state/text + source cards."""
        record = self.get_record(run_id, owner_id=owner_id)
        runtime = self._runtime_for(record)
        rows = runtime.article.storage.read_sections()
        rows.sort(key=lambda row: (row.order, row.id))
        assembled = assemble(
            [section_from(row.state(), row.text) for row in rows],
            title=record.brief.topic if record.brief is not None else None,
        )
        return {
            "markdown": assembled.markdown,
            "body_words": assembled.body_words,
            "words_by_section": dict(assembled.words_by_section),
            "sections": [
                {**row.state().model_dump(mode="json", by_alias=True), "text": row.text}
                for row in rows
            ],
            "sources": [
                card.model_dump(mode="json", by_alias=True)
                for card in runtime.memory.sources.list_sources()
            ],
        }

    def usage_summary(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Token usage by role and iteration, planner included."""
        record = self.get_record(run_id, owner_id=owner_id)
        by_role: dict[str, TokenUsage] = {}
        by_iter: dict[str, TokenUsage] = {}
        for entry in record.entries:
            role = entry.role_name
            by_role[role] = _usage_add(by_role.get(role, _zero_usage()), entry.tokens)
            key = str(entry.iter)
            by_iter[key] = _usage_add(by_iter.get(key, _zero_usage()), entry.tokens)

        def dump(usage: TokenUsage) -> dict[str, int]:
            return {"in": usage.in_, "out": usage.out, "cached": usage.cached, "total": usage.total}

        return {
            "run_id": record.id,
            "total": record.total_tokens_spent(),
            "agents_total": dump(record.agent_tokens()),
            "planner": dump(record.planner_tokens),
            "by_role": {role: dump(usage) for role, usage in sorted(by_role.items())},
            "by_iter": {key: dump(usage) for key, usage in sorted(by_iter.items())},
            "token_cap": self._policies.run_token_cap + record.extra_token_budget,
            "iterations": record.iter,
            "max_iterations": self._policies.max_iterations + record.extra_iterations,
        }

    # -- read models for the API (Step 9b item 6, the console views) -------------
    def events_page(
        self, run_id: str, *, after: int = -1, limit: int = 200, owner_id: str | None = None
    ) -> dict[str, Any]:
        """Persisted events as a JSON page - the SSE log's offline twin.

        ``next_after`` is the cursor to pass as ``after`` on the next call, or
        ``None`` when the log is exhausted (fewer than ``limit`` events left).
        """
        record = self.get_record(run_id, owner_id=owner_id)
        events = [event for event in record.events if event.seq > after]
        page = events[:limit]
        next_after = page[-1].seq if page and len(events) > limit else None
        return {"run_id": record.id, "events": page, "next_after": next_after}

    def quality_view(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Latest validator report + the Q series the erosion detectors read."""
        record = self.get_record(run_id, owner_id=owner_id)
        latest = record.reports[-1] if record.reports else None
        return {
            "run_id": record.id,
            "latest": latest,
            "series": [
                {
                    "iter": report.iter,
                    "q": report.Q,
                    "gates_passed": report.all_gates_pass,
                    "verified_citation_ratio": report.verified_citation_ratio,
                }
                for report in record.reports
            ],
            "best_q": record.best_q,
        }

    def _storage_rows(self, runtime: _Runtime, section_id: str) -> list[StoredVersion]:
        return sorted(
            runtime.article.storage.read_versions(section_id), key=lambda row: row.version
        )

    async def section_history(
        self, run_id: str, section_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any]:
        """One section's immutable versions, oldest first. Bodies stay in the store."""
        record = self.get_record(run_id, owner_id=owner_id)
        runtime = self._runtime_for(record)
        await runtime.article.get_section(section_id)  # 404 not_found when unknown
        return {
            "run_id": record.id,
            "section_id": section_id,
            "versions": [
                {
                    "section_id": row.section_id,
                    "version": row.version,
                    "hash": row.hash,
                    "words": row.words,
                    "status": row.status.value,
                    "chars_added": row.chars_added,
                    "chars_removed": row.chars_removed,
                    "patch_id": row.patch_id,
                    "entry_id": row.entry_id,
                    "created_at": row.created_at,
                    "q_after": row.q_after,
                }
                for row in self._storage_rows(runtime, section_id)
            ],
        }

    async def section_diff(
        self,
        run_id: str,
        section_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Unified diff + change sizes between two versions of one section.

        Version 0 is the empty baseline every section starts from, so asking
        for ``0 -> 1`` shows the first draft as pure additions. ``from``
        defaults to ``to - 1`` (0 for the first text) and ``to`` to the head,
        per the OpenAPI contract. ``from > to`` is a ``run_conflict``: the
        versions exist but the range is not renderable.

        Raises:
            NotFoundError: unknown section or a version beyond the head.
            RunConflict: ``from`` names a later version than ``to``.
        """
        record = self.get_record(run_id, owner_id=owner_id)
        runtime = self._runtime_for(record)
        row = await runtime.article.get_section(section_id)  # 404 when unknown
        head = row.version
        resolved_to = head if to_version is None else to_version
        if resolved_to > head:
            raise NotFoundError(
                f"section '{section_id}' has no version {resolved_to} (head is {head})",
                section_id=section_id,
                version=resolved_to,
            )
        resolved_from = max(0, resolved_to - 1) if from_version is None else from_version
        if resolved_from > resolved_to:
            raise RunConflict(
                f"cannot diff version {resolved_from} against earlier version "
                f"{resolved_to}: 'from' must not exceed 'to'",
                run_id=run_id,
                reason="invalid_version_range",
                section_id=section_id,
                from_version=resolved_from,
                to_version=resolved_to,
            )

        rows = {stored.version: stored for stored in self._storage_rows(runtime, section_id)}

        def side(version: int) -> dict[str, Any]:
            if version == 0:
                return {"version": 0, "hash": EMPTY_SECTION_HASH, "words": 0, "text": ""}
            stored = rows.get(version)
            if stored is None:  # pragma: no cover - head bounds make this unreachable
                raise NotFoundError(
                    f"section '{section_id}' has no version {version}",
                    section_id=section_id,
                    version=version,
                )
            return {
                "version": stored.version,
                "hash": stored.hash,
                "words": stored.words,
                "text": stored.text,
            }

        left, right = side(resolved_from), side(resolved_to)
        diff = diff_texts(
            left["text"],
            right["text"],
            from_label=f"{section_id}@{resolved_from}",
            to_label=f"{section_id}@{resolved_to}",
        )
        return {
            "run_id": record.id,
            "section_id": section_id,
            "from": {"version": left["version"], "hash": left["hash"], "words": left["words"]},
            "to": {"version": right["version"], "hash": right["hash"], "words": right["words"]},
            "diff": {
                "stats": {
                    "chars_added": diff.stats.chars_added,
                    "chars_removed": diff.stats.chars_removed,
                    "net_chars": diff.stats.net_chars,
                    "touched_chars": diff.stats.touched_chars,
                },
                "lines_added": diff.lines_added,
                "lines_removed": diff.lines_removed,
                "unified": diff.unified,
            },
        }

    async def snapshots_list(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Whole-article snapshots, newest first, plus the run's best pointer."""
        record = self.get_record(run_id, owner_id=owner_id)
        runtime = self._runtime_for(record)
        best = record.best_snapshot_id
        if best is None:
            measured = await runtime.article.best_snapshot()
            best = None if measured is None else measured.id
        return {
            "run_id": record.id,
            "best_snapshot_id": best,
            "snapshots": [
                {
                    "id": snapshot.id,
                    "label": snapshot.label,
                    "q": snapshot.q,
                    "iter": snapshot.iter,
                    "created_at": snapshot.created_at,
                    "section_ids": list(snapshot.section_ids),
                    "total_words": snapshot.total_words,
                }
                for snapshot in await runtime.article.snapshots()
            ],
        }

    async def restore_snapshot(
        self, run_id: str, snapshot_id: str, *, dry_run: bool = False, owner_id: str | None = None
    ) -> dict[str, Any]:
        """Roll the article back to a snapshot as new versions (history is append-only).

        Refuses while the run is executing (a wave could interleave patches
        with the restore). ``dry_run=True`` reports exactly what a real
        restore would change - which sections differ and which version each
        restored section would get - without writing anything.

        Raises:
            RunConflict: the run is executing right now.
            NotFoundError: no snapshot with that id belongs to this run.
        """
        record = self.get_record(run_id, owner_id=owner_id)
        if self.is_running(run_id) or record.state is RunStateName.EXECUTING:
            raise RunConflict(
                "run is executing; restore when the current wave finishes",
                run_id=run_id,
                reason="restore_while_running",
                state=record.state.value,
            )
        runtime = self._runtime_for(record)

        if dry_run:
            snapshot = runtime.article.storage.read_snapshot(snapshot_id)
            if snapshot is None:
                raise NotFoundError(f"no snapshot '{snapshot_id}'", snapshot_id=snapshot_id)
            restored: list[str] = []
            unchanged: list[str] = []
            versions: dict[str, int] = {}
            for part in snapshot.sections:
                row = runtime.article.storage.read_section(part.state.id)
                head_text = "" if row is None else row.text
                if content_hash(head_text) == content_hash(part.text):
                    unchanged.append(part.state.id)
                else:
                    restored.append(part.state.id)
                    versions[part.state.id] = (0 if row is None else row.version) + 1
            return {
                "run_id": record.id,
                "snapshot_id": snapshot.id,
                "label": snapshot.label,
                "q": snapshot.q,
                "restored": restored,
                "unchanged": unchanged,
                "versions": versions,
                "state": record.state,
                "updated_at": record.updated_at,
                "dry_run": True,
            }

        result = await runtime.article.restore(snapshot_id)
        self._machine.emit(
            record,
            "rollback_applied",
            detail={"snapshot_id": result.snapshot_id, "restored": list(result.restored)},
            save=False,
        )
        await self._persist(record, runtime)
        return {
            "run_id": record.id,
            "snapshot_id": result.snapshot_id,
            "label": result.label,
            "q": result.q,
            "restored": list(result.restored),
            "unchanged": list(result.unchanged),
            "versions": dict(result.versions),
            "state": record.state,
            "updated_at": record.updated_at,
            "dry_run": False,
        }


# --------------------------------------------------------------------------- background runs
class RunManager:
    """Background execution for the API: one ``asyncio.Task`` per active run."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        self._tasks: dict[str, asyncio.Task[RunRecord]] = {}

    @property
    def orchestrator(self) -> Orchestrator:
        """The orchestrator being driven in the background."""
        return self._orchestrator

    def is_running(self, run_id: str) -> bool:
        """True while a background task for ``run_id`` is alive."""
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def start(self, run_id: str) -> bool:
        """Start (or resume) ``run_id`` in the background. False if already running."""
        if self.is_running(run_id):
            return False
        task = asyncio.create_task(self._orchestrator.run(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(partial(self._finished, run_id))
        return True

    def _finished(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        if task.cancelled():
            logger.warning("background run cancelled", extra={"run_id": run_id})
            return
        exc = task.exception()
        if exc is not None:
            logger.error("background run failed: %s", redact(str(exc)), extra={"run_id": run_id})
            try:
                record = self._orchestrator.get_record(run_id)
                record.last_error = redact(f"background run failed: {exc}")[:400]
                self._orchestrator.store.save(record)
            except ScholarLoopError:
                pass

    async def wait(self, run_id: str) -> RunRecord:
        """Await the background task when present; else load the record."""
        task = self._tasks.get(run_id)
        if task is not None:
            return await task
        return self._orchestrator.get_record(run_id)
