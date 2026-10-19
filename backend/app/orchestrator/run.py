"""Orchestrator - one deterministic run of the whole loop.

Plain Python drives; the LLM only plans and (through agents) executes
(ADR-0001). One iteration is one full pass over the current ``Plan``:

``EXECUTING`` ready-task waves (``max_parallel`` at a time, patches applied
serially in plan order so a wave is deterministic), ``VALIDATING`` off the
validator's ``QualityReport``, ``CHECKPOINT`` (article snapshot + bounded
digest rebuild), ``DECIDING`` via the :class:`LoopController`, and on
``CONTINUE`` a ``REPLANNING`` pass where the Planner sees only the digest.

Nothing an agent returns is trusted: ownership (task/role), the task token
budget and the source-verification guard are re-checked here, patches are
applied by the article store against ``base_version`` (stale or non-matching
patches are recorded as failed entries, never forced through), and every
outcome - including an executor raising - becomes a ledger entry the
controller can count. Planner tokens are billed to the run even though the
planner writes no ``ChangeEntry`` rows.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence

from app.article import ArticleStore, recurred_sections
from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, ChangeStatus, LoopAction
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.errors import BudgetExceeded, NotFoundError, ScholarLoopError
from app.core.ids import new_ulid
from app.core.policies import DEFAULT_POLICIES, Policies
from app.core.rubric import Rubric, SectionSpec, resolve_sections
from app.memory import MemoryStore
from app.orchestrator.controller import LoopController, LoopState
from app.orchestrator.errors import InvalidRunStateError, OrchestratorError
from app.orchestrator.state import (
    AgentExecutor,
    ExecutorRegistry,
    RunOutcome,
    RunPhase,
    RunStatus,
    TaskContext,
)
from app.planner.client import PlannerPort

__all__ = ["Orchestrator"]

logger = logging.getLogger(__name__)

#: Roles whose returned section states may promote/annotate the article store.
_SECTION_STATE_ROLES: frozenset[AgentRole] = frozenset({AgentRole.REVIEWER, AgentRole.VALIDATOR})

_EMPTY_USAGE = TokenUsage(**{"in": 0, "out": 0, "cached": 0})


class Orchestrator:
    """Runs one brief to a terminal (or paused) :class:`RunOutcome`.

    One instance runs once: the ledger, snapshots and counters are the run's
    own, and a second brief deserves a fresh orchestrator rather than a reset
    that would make two runs share one history.
    """

    def __init__(
        self,
        *,
        planner: PlannerPort,
        executors: AgentExecutor | Mapping[AgentRole, AgentExecutor] | ExecutorRegistry,
        article: ArticleStore | None = None,
        memory: MemoryStore | None = None,
        rubric: Rubric | None = None,
        policies: Policies = DEFAULT_POLICIES,
        controller: LoopController | None = None,
        evaluate_with_planner: bool = True,
        rollback_on_erosion: bool = True,
        snapshot_each_iteration: bool = True,
        id_factory: Callable[[], str] = new_ulid,
    ) -> None:
        self._planner = planner
        self._executors = self._coerce_registry(executors)
        self._article = article if article is not None else ArticleStore()
        self._memory = memory if memory is not None else MemoryStore(policies)
        self._rubric = rubric
        self._policies = policies
        self._controller = controller if controller is not None else LoopController(policies)
        self._evaluate_with_planner = evaluate_with_planner
        self._rollback_on_erosion = rollback_on_erosion
        self._snapshot_each_iteration = snapshot_each_iteration
        self._new_id = id_factory

        self._phase = RunPhase.INTAKE
        self._started = False
        self._stop_requested = False
        self._brief: Brief | None = None
        self._sections: tuple[SectionSpec, ...] = ()
        self._decisions: list[LoopDecision] = []
        self._q_history: list[int] = []
        self._iteration_tokens: list[int] = []
        self._spend_at_last_decision = 0
        self._digest_text = ""

    # -- public surface ---------------------------------------------------------

    @property
    def current_phase(self) -> RunPhase:
        """Where the state machine currently sits (for status surfaces)."""
        return self._phase

    @property
    def article(self) -> ArticleStore:
        """The article store this run writes into."""
        return self._article

    @property
    def memory(self) -> MemoryStore:
        """The run memory (ledger, sources, digest) this run records into."""
        return self._memory

    def request_stop(self) -> None:
        """Ask the run to stop at the next wave/decision boundary (``STOP_USER``)."""
        self._stop_requested = True

    async def markdown(self, *, title: str | None = None) -> str:
        """The current article assembled as markdown (title defaults to the topic)."""
        resolved_title = title or (self._brief.topic if self._brief is not None else None)
        return await self._article.to_markdown(title=resolved_title)

    async def run(self, brief: Brief) -> RunOutcome:
        """Run ``brief`` through plan/execute/validate/decide until the loop ends."""
        if self._started:
            raise InvalidRunStateError(
                "an Orchestrator runs one brief; construct a new one per run"
            )
        self._started = True
        self._brief = brief

        self._phase = RunPhase.INTAKE
        self._memory.set_brief(brief)
        await self._ensure_sections(brief)

        self._phase = RunPhase.PLANNING
        try:
            plan = await self._planner.plan(
                brief, digest_text="", sections=self._sections, version=1
            )
        except ScholarLoopError as exc:
            return await self._finish_planner_failure(exc, iteration=0)
        self._memory.set_plan(plan)

        iteration = 0
        while True:
            iteration += 1
            if iteration > self._policies.max_iterations + 1:  # pragma: no cover - guard rail
                raise OrchestratorError("loop exceeded max_iterations without a decision")

            self._phase = RunPhase.EXECUTING
            results = await self._execute_plan(plan, iteration=iteration)

            self._phase = RunPhase.VALIDATING
            report = self._latest_report(results)
            if report is not None:
                self._q_history.append(report.Q)
                await self._article.record_quality(float(report.Q))
                if self._snapshot_each_iteration:
                    await self._article.snapshot(
                        f"iteration-{iteration}", q=float(report.Q), iter=iteration
                    )

            self._phase = RunPhase.CHECKPOINT
            self._memory.update_sections(await self._article.list_state())
            self._digest_text = self._memory.build_digest().markdown

            self._phase = RunPhase.DECIDING
            verdict = await self._evaluate(brief, report)
            decision = self._controller.decide(
                await self._loop_state(iteration=iteration, report=report, verdict=verdict)
            )
            self._decisions.append(decision)
            self._bookkeep_spend()
            logger.info(
                "iteration %s decision %s reasons=%s",
                iteration,
                decision.action.value,
                ",".join(decision.reasons),
            )

            if decision.action is LoopAction.CONTINUE:
                self._phase = RunPhase.REPLANNING
                try:
                    plan = await self._planner.replan(
                        brief,
                        digest_text=self._digest_text,
                        sections=self._sections,
                        version=plan.version + 1,
                    )
                except ScholarLoopError as exc:
                    return await self._finish_planner_failure(exc, iteration=iteration)
                self._memory.set_plan(plan)
                continue

            rolled_back = False
            if decision.action is LoopAction.STOP_EROSION:
                rolled_back = await self._maybe_roll_back()
            status = RunStatus.from_action(decision.action)
            self._phase = (
                RunPhase.DONE
                if decision.action is LoopAction.OFFER_FINISH
                else RunPhase.PAUSED
                if decision.action is LoopAction.ASK_USER
                else RunPhase.STOPPED
            )
            return await self._outcome(
                status, iterations=iteration, plan_version=plan.version, rolled_back=rolled_back
            )

    # -- intake / planning -------------------------------------------------------

    async def _ensure_sections(self, brief: Brief) -> None:
        """Create the rubric's sections when the article store starts empty."""
        if self._rubric is not None:
            self._sections = tuple(resolve_sections(self._rubric, brief.article_type))
        if self._article.section_ids():
            self._memory.update_sections(await self._article.list_state())
            return
        if not self._sections:
            raise OrchestratorError(
                "the article store is empty and no rubric was supplied, "
                "so there are no sections for agents to write into"
            )
        await self._article.create_sections(list(self._sections))
        self._memory.update_sections(await self._article.list_state())

    async def _evaluate(self, brief: Brief, report: QualityReport | None) -> Verdict | None:
        """Ask the Planner for advice. Its failure never blocks a decision."""
        if not self._evaluate_with_planner or report is None:
            return None
        try:
            return await self._planner.evaluate(brief, report, digest_text=self._digest_text)
        except ScholarLoopError as exc:
            # The controller decides from numbers; a failed verdict call is a
            # missing opinion, not a failed run. The replan attempt that may
            # follow will surface a persistent planner outage properly.
            logger.warning("planner evaluation failed (%s); deciding without a verdict", exc.code)
            return None

    # -- execution -----------------------------------------------------------------

    async def _execute_plan(self, plan: Plan, *, iteration: int) -> list[AgentResult]:
        """Run every reachable task in dependency waves, recording every outcome."""
        assert self._brief is not None
        completed: set[str] = set()
        attempted: set[str] = set()
        recorded: list[AgentResult] = []
        semaphore = asyncio.Semaphore(self._policies.max_parallel)
        digest_at_plan_start = self._digest_text

        while not self._stop_requested and not self._over_budget():
            ready = [task for task in plan.ready_tasks(completed) if task.id not in attempted]
            if not ready:
                break
            # Execution is parallel; application below is serial, in plan order.
            wave = await asyncio.gather(
                *(
                    self._run_one(
                        task,
                        iteration=iteration,
                        digest_text=digest_at_plan_start,
                        plan_version=plan.version,
                        semaphore=semaphore,
                    )
                    for task in ready
                )
            )
            for task, raw in zip(ready, wave, strict=True):
                attempted.add(task.id)
                result = await self._record(task, raw, iteration=iteration)
                recorded.append(result)
                if result.entry.is_success:
                    completed.add(task.id)
        return recorded

    async def _run_one(
        self,
        task: Task,
        *,
        iteration: int,
        digest_text: str,
        plan_version: int,
        semaphore: asyncio.Semaphore,
    ) -> AgentResult:
        """Execute one task under the semaphore; every failure becomes a result."""
        executor = self._executors.get(task.role)
        if executor is None:
            return self._failed_result(
                task,
                iteration=iteration,
                summary=f"No executor registered for role {task.role.value}.",
            )
        async with semaphore:
            try:
                context = await self._context_for(
                    task, iteration=iteration, digest_text=digest_text, plan_version=plan_version
                )
                return await executor.execute(task, context)
            except ScholarLoopError as exc:
                logger.warning("task %s (%s) failed: %s", task.id, task.role.value, exc.code)
                return self._failed_result(
                    task,
                    iteration=iteration,
                    summary=f"Task failed with error {exc.code}: {_bounded(exc.message)}",
                )
            except Exception as exc:  # executor code is not trusted with the run's life
                logger.exception("task %s (%s) raised unexpectedly", task.id, task.role.value)
                return self._failed_result(
                    task,
                    iteration=iteration,
                    summary=f"Task raised unexpectedly: {type(exc).__name__}.",
                )

    async def _context_for(
        self, task: Task, *, iteration: int, digest_text: str, plan_version: int
    ) -> TaskContext:
        """Resolve exactly the references this task carries - nothing wider."""
        assert self._brief is not None
        states = {state.id: state for state in await self._article.list_state()}
        sections = tuple(states[sid] for sid in task.section_ids if sid in states)
        texts: dict[str, str] = {}
        for section in sections:
            try:
                texts[section.id] = await self._article.get_text(section.id)
            except NotFoundError:  # pragma: no cover - state said it exists
                continue
        cards: list[SourceCard] = []
        for source_id in task.source_ids:
            card = self._memory.sources.get(source_id)
            if card is not None:
                cards.append(card)
        return TaskContext(
            brief=self._brief,
            task=task,
            iteration=iteration,
            plan_version=plan_version,
            digest_text=digest_text,
            sections=sections,
            section_texts=texts,
            source_cards=tuple(cards),
        )

    # -- recording -------------------------------------------------------------------

    async def _record(self, task: Task, result: AgentResult, *, iteration: int) -> AgentResult:
        """Verify, apply and ledger one raw result. Returns what was recorded."""
        entry = result.entry
        if entry.task_id != task.id or entry.role is not task.role:
            logger.warning(
                "discarding result that does not own task %s (entry task %s, role %s)",
                task.id,
                entry.task_id,
                entry.role.value,
            )
            rejected = self._failed_result(
                task,
                iteration=iteration,
                summary="Result discarded: its entry does not own this task or role.",
                tokens=entry.tokens,
            )
            self._memory.ledger.append(rejected.entry)
            return rejected

        recorded = result
        if entry.status is ChangeStatus.OK:
            recorded = await self._apply(task, result)

        final = self._with_ledger_identity(recorded, iteration=iteration)
        self._memory.ledger.append(final.entry)
        if final.report is not None and final.entry.status is ChangeStatus.OK:
            self._memory.record_report(final.report)
        return final

    async def _apply(self, task: Task, result: AgentResult) -> AgentResult:
        """Apply a successful result's effects, or demote it to ``failed``."""
        entry = result.entry
        if entry.tokens.total > task.token_budget:
            return self._demoted(
                result,
                summary=(
                    f"Result rejected: spent {entry.tokens.total} tokens over the "
                    f"task budget of {task.token_budget}."
                ),
            )
        # Sources first: a rejected card must leave the article untouched.
        if result.source_cards:
            try:
                self._memory.sources.ingest_many(result.source_cards, role=entry.role)
            except ScholarLoopError as exc:
                logger.warning("source ingest rejected for task %s: %s", task.id, exc.code)
                return self._demoted(
                    result, summary=f"Source cards rejected ({exc.code}); result not applied."
                )
        for patch in result.patches:
            try:
                await self._article.apply_patch(patch, entry_id=entry.id)
            except ScholarLoopError as exc:
                # StalePatch / PatchRejected / unknown section: the patch is
                # discarded (never merged, never forced) and the plan gets to
                # re-task the work from the digest on the next iteration.
                logger.warning("patch for section %s rejected: %s", patch.section_id, exc.code)
                return self._demoted(
                    result,
                    summary=f"Patch for {patch.section_id} rejected ({exc.code}); not applied.",
                )
        if entry.role in _SECTION_STATE_ROLES:
            await self._apply_section_states(result.sections)
        return result

    async def _apply_section_states(self, sections: Sequence[SectionState]) -> None:
        """Promote/annotate sections a reviewer or validator ruled on."""
        for state in sections:
            try:
                await self._article.set_open_issues(state.id, list(state.open_issues))
                if state.version > 0:
                    await self._article.set_status(state.id, state.status)
            except ScholarLoopError as exc:
                # e.g. promoting a still-empty section: the store's contract
                # wins, the agent learns from the next digest.
                logger.info("section state for %s not applied: %s", state.id, exc.code)

    def _demoted(self, result: AgentResult, *, summary: str) -> AgentResult:
        """Copy a result with its entry marked failed and an honest summary."""
        entry = result.entry.model_copy(
            update={"status": ChangeStatus.FAILED, "summary": _bounded(summary)}, deep=True
        )
        return result.model_copy(update={"entry": entry, "report": None}, deep=True)

    def _with_ledger_identity(self, result: AgentResult, *, iteration: int) -> AgentResult:
        """Stamp the orchestrator's iteration and a unique entry id before append."""
        entry = result.entry
        updates: dict[str, object] = {}
        if entry.iter != iteration:
            updates["iter"] = iteration
        if self._memory.ledger.get(entry.id) is not None:
            updates["id"] = self._new_id()
        if not updates:
            return result
        return result.model_copy(
            update={"entry": entry.model_copy(update=updates, deep=True)}, deep=True
        )

    def _failed_result(
        self,
        task: Task,
        *,
        iteration: int,
        summary: str,
        tokens: TokenUsage = _EMPTY_USAGE,
    ) -> AgentResult:
        """A synthesized failed result for work that never produced one itself."""
        return AgentResult(
            entry=ChangeEntry(
                id=self._new_id(),
                iter=iteration,
                task_id=task.id,
                role=task.role,
                targets=list(task.section_ids),
                summary=_bounded(summary),
                tokens=tokens,
                status=ChangeStatus.FAILED,
            )
        )

    # -- deciding / finishing -----------------------------------------------------------

    @staticmethod
    def _latest_report(results: Sequence[AgentResult]) -> QualityReport | None:
        """The last validator report of the iteration, or ``None``."""
        for result in reversed(results):
            if result.report is not None and result.entry.is_success:
                return result.report
        return None

    def _run_spend(self) -> int:
        """Everything spent so far: ledger (agents) plus the planner's own calls."""
        return self._memory.ledger.total_tokens().total + self._planner.tokens_spent

    def _over_budget(self) -> bool:
        """True when a cap is reached, before starting another wave.

        This is a seatbelt, not the governor: it only refuses to *start* more
        work once the run cap - or this iteration's share of it, measured as
        spend since the last decision - is already spent.
        """
        spend = self._run_spend()
        iteration_spend = spend - self._spend_at_last_decision
        return (
            spend >= self._policies.run_token_cap
            or iteration_spend >= self._policies.iteration_token_cap
        )

    def _bookkeep_spend(self) -> None:
        """Bucket spend since the last decision into ``iteration_tokens``."""
        spend = self._run_spend()
        self._iteration_tokens.append(max(0, spend - self._spend_at_last_decision))
        self._spend_at_last_decision = spend

    async def _loop_state(
        self, *, iteration: int, report: QualityReport | None, verdict: Verdict | None
    ) -> LoopState:
        """Snapshot the numbers the controller may look at for this decision."""
        ledger = self._memory.ledger
        issues = ledger.issue_summary()
        histories = {
            section_id: await self._article.history(section_id)
            for section_id in self._article.section_ids()
        }
        iteration_spend = max(0, self._run_spend() - self._spend_at_last_decision)
        entries_this_iteration = ledger.by_iteration(iteration)
        return LoopState(
            iteration=iteration,
            q_history=tuple(float(q) for q in self._q_history),
            iteration_tokens=(*self._iteration_tokens, iteration_spend),
            run_tokens=self._run_spend(),
            report=report,
            verdict=verdict,
            error_streak=ledger.failed_streak(),
            issues_opened=issues.total_opened,
            issues_closed=issues.total_closed,
            issue_churn_ratio=issues.churn_ratio,
            needs_user_input=any(
                e.status is ChangeStatus.NEEDS_USER_INPUT for e in entries_this_iteration
            ),
            user_stop=self._stop_requested,
            recurred_sections=tuple(
                recurred_sections(histories, window=max(2, self._policies.plateau.window + 1))
            ),
        )

    async def _maybe_roll_back(self) -> bool:
        """Restore the best snapshot when erosion left the article worse than it was."""
        if not self._rollback_on_erosion:
            return False
        best = await self._article.best_snapshot()
        if best is None or best.q is None:
            return False
        if self._q_history and best.q <= self._q_history[-1]:
            return False
        await self._article.restore(best.id)
        self._memory.update_sections(await self._article.list_state())
        self._digest_text = self._memory.build_digest().markdown
        logger.info("rolled back to best snapshot %s (Q=%s)", best.id, best.q)
        return True

    async def _finish_planner_failure(
        self, error: ScholarLoopError, *, iteration: int
    ) -> RunOutcome:
        """End the run with a decision when the Planner itself cannot answer."""
        is_budget = isinstance(error, BudgetExceeded)
        action = LoopAction.STOP_BUDGET if is_budget else LoopAction.STOP_ERRORS
        reason = "planner_prompt_over_budget" if is_budget else "planner_failed"
        decision = LoopDecision(
            iter=iteration,
            action=action,
            reasons=[reason],
            evidence={"error_code": error.code, "run_tokens": self._run_spend()},
        )
        self._decisions.append(decision)
        logger.warning("planner failed at iteration %s: %s", iteration, error.code)
        self._phase = RunPhase.STOPPED
        self._memory.update_sections(await self._article.list_state())
        self._digest_text = self._memory.build_digest().markdown
        plan_version = self._memory.plan.version if self._memory.plan else 1
        return await self._outcome(
            RunStatus.from_action(action), iterations=iteration, plan_version=plan_version
        )

    async def _outcome(
        self,
        status: RunStatus,
        *,
        iterations: int,
        plan_version: int,
        rolled_back: bool = False,
    ) -> RunOutcome:
        """Assemble the run outcome from the stores (references, not bodies)."""
        best = await self._article.best_snapshot()
        questions = tuple(entry.summary for entry in self._memory.ledger.needs_user_input_entries())
        agent_tokens = self._memory.ledger.total_tokens().total
        return RunOutcome(
            status=status,
            phase=self._phase,
            iterations=iterations,
            decisions=tuple(self._decisions),
            q_history=tuple(self._q_history),
            run_tokens=agent_tokens + self._planner.tokens_spent,
            planner_tokens=self._planner.tokens_spent,
            agent_tokens=agent_tokens,
            final_plan_version=plan_version,
            digest_text=self._digest_text,
            sections=tuple(await self._article.list_state()),
            reports=tuple(self._memory.reports),
            best_snapshot_id=best.id if best is not None else None,
            best_q=best.q if best is not None else None,
            rolled_back=rolled_back,
            questions=questions,
        )

    @staticmethod
    def _coerce_registry(
        executors: AgentExecutor | Mapping[AgentRole, AgentExecutor] | ExecutorRegistry,
    ) -> ExecutorRegistry:
        """Accept a registry, a role mapping, or one executor for every role."""
        if isinstance(executors, ExecutorRegistry):
            return executors
        if isinstance(executors, Mapping):
            return ExecutorRegistry(dict(executors))
        return ExecutorRegistry(dict.fromkeys(AgentRole, executors))


def _bounded(summary: str) -> str:
    """Keep a synthesized ledger summary inside the 60-word contract bound."""
    words = " ".join(summary.split()).split()
    if not words:
        return "No detail provided."
    return " ".join(words[:60])
