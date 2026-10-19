"""Orchestrator tests: full loop runs over scripted planners and executors.

No live provider anywhere: the planner is either a queue-backed fake or the
real ``LLMPlanner`` over ``MockProvider``; executors are deterministic fakes
that return contract-shaped results (or raise, or misbehave) so the run's
trust checks, wave scheduling, patch application, snapshots, rollback and
terminal mapping are all exercised end to end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from app.article import ArticleStore
from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import (
    AgentRole,
    ChangeStatus,
    LoopAction,
    PatchOpType,
    SectionStatus,
    VerificationStatus,
)
from app.contracts.ledger import ChangeEntry, TokenUsage, patch_ref_for
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.policies import Policies
from app.core.rubric import Rubric, SectionSpec
from app.memory import MemoryStore
from app.orchestrator import Orchestrator, RunPhase, RunStatus, TaskContext
from app.orchestrator.errors import InvalidRunStateError
from app.planner import LLMPlanner
from app.providers.base import estimate_tokens
from app.providers.errors import ProviderTimeout
from app.providers.mock import MockProvider, Script
from tests.factories import SAMPLE_BRIEF, sample_ulid

CHAIN = (
    AgentRole.ARCHITECT,
    AgentRole.RESEARCHER,
    AgentRole.WRITER,
    AgentRole.REVIEWER,
    AgentRole.VALIDATOR,
)

FIRST_DRAFT = "Token budgets keep loops honest. Evidence guides every claim (Nguyen, 2024)."


def make_brief() -> Brief:
    return Brief.model_validate(dict(SAMPLE_BRIEF))


def usage(tokens_in: int = 100, tokens_out: int = 50) -> TokenUsage:
    return TokenUsage.model_validate({"in": tokens_in, "out": tokens_out, "cached": 0})


def make_task(
    seed: int,
    role: AgentRole,
    *,
    depends_on: tuple[str, ...] = (),
    token_budget: int = 5_000,
) -> Task:
    return Task(
        id=sample_ulid(seed),
        title=f"{role.value} step",
        role=role,
        section_ids=["introduction"],
        instructions=f"Do the {role.value} work.",
        acceptance=["Acceptance satisfied"],
        token_budget=token_budget,
        depends_on=list(depends_on),
    )


def make_plan(*, version: int = 1, seed_base: int = 100) -> Plan:
    tasks: list[Task] = []
    previous: str | None = None
    for index, role in enumerate(CHAIN):
        task = make_task(seed_base + index, role, depends_on=(previous,) if previous else ())
        tasks.append(task)
        previous = task.id
    return Plan(version=version, goal="Finish the article.", tasks=tasks)


def make_report(*, q: int, iteration: int, failed: tuple[str, ...] = ()) -> QualityReport:
    return QualityReport(
        iter=iteration,
        gates={name: name not in failed for name in GATE_NAMES},
        Q=q,
        verified_citation_ratio=1.0,
    )


def entry_for(
    task: Task,
    *,
    seed: int,
    iteration: int = 1,
    status: ChangeStatus = ChangeStatus.OK,
    summary: str = "Did the work.",
    tokens: TokenUsage | None = None,
    patch_ref: str | None = None,
    role: AgentRole | None = None,
) -> ChangeEntry:
    return ChangeEntry(
        id=sample_ulid(seed),
        iter=iteration,
        task_id=task.id,
        role=role or task.role,
        targets=list(task.section_ids),
        summary=summary,
        patch_ref=patch_ref,
        tokens=tokens or usage(),
        status=status,
    )


def source_card(seed: int = 900) -> SourceCard:
    return SourceCard(
        id=sample_ulid(seed),
        title="Budgeted orchestration for agent writing loops",
        authors=["Nguyen, L."],
        year=2024,
        venue="Journal of Applied NLP Systems",
        doi="10.1234/jans.2024.00042",
        url="https://example.org/jans/2024/42",
        snippet="A bounded controller keeps prompt growth predictable while quality improves.",
    )


class FakePlanner:
    """Queue-backed PlannerPort: plans are served in order, verdicts computed."""

    def __init__(
        self,
        plans: Sequence[Plan | BaseException],
        *,
        replan_error: BaseException | None = None,
        tokens_per_call: int = 7,
    ) -> None:
        self._plans = list(plans)
        self._replan_error = replan_error
        self._tokens_per_call = tokens_per_call
        self._spent = 0
        self.plan_calls: list[int] = []
        self.replan_digests: list[str] = []
        self.evaluate_calls = 0

    @property
    def tokens_spent(self) -> int:
        return self._spent

    async def plan(
        self,
        brief: Brief,
        *,
        digest_text: str = "",
        sections: Sequence[SectionSpec] = (),
        version: int = 1,
    ) -> Plan:
        _ = (brief, digest_text, sections)
        self._spent += self._tokens_per_call
        self.plan_calls.append(version)
        item = self._plans.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def replan(
        self,
        brief: Brief,
        *,
        digest_text: str,
        sections: Sequence[SectionSpec] = (),
        version: int,
    ) -> Plan:
        _ = sections
        self._spent += self._tokens_per_call
        self.replan_digests.append(digest_text)
        if self._replan_error is not None:
            raise self._replan_error
        item = self._plans.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert item.version == version, "test bug: replan served the wrong plan version"
        return item

    async def evaluate(
        self, brief: Brief, report: QualityReport, *, digest_text: str = ""
    ) -> Verdict:
        _ = (brief, digest_text)
        self._spent += self._tokens_per_call
        self.evaluate_calls += 1
        if report.all_gates_pass and report.Q >= 70:
            return Verdict(meets_minimum=True, recommendation="finish")
        return Verdict(
            meets_minimum=False, missing=["Improve measured quality"], recommendation="continue"
        )


Handler = Callable[[Task, TaskContext], AgentResult]


class FakeExecutor:
    """One executor for every role, with per-role override hooks."""

    def __init__(
        self,
        *,
        reports: dict[int, QualityReport] | None = None,
        overrides: dict[AgentRole, Handler] | None = None,
        entry_tokens: TokenUsage | None = None,
        delay: float = 0.0,
    ) -> None:
        self._reports = reports or {1: make_report(q=85, iteration=1)}
        self._overrides = overrides or {}
        self._tokens = entry_tokens or usage()
        self._delay = delay
        self._seed = 10_000
        self.calls: list[AgentRole] = []
        self.active = 0
        self.max_active = 0

    def next_seed(self) -> int:
        self._seed += 1
        return self._seed

    async def execute(self, task: Task, context: TaskContext) -> AgentResult:
        self.calls.append(task.role)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            override = self._overrides.get(task.role)
            if override is not None:
                return override(task, context)
            return self._default(task, context)
        finally:
            self.active -= 1

    def _default(self, task: Task, context: TaskContext) -> AgentResult:
        role = task.role
        if role is AgentRole.RESEARCHER:
            return AgentResult(entry=self._entry(task, context), source_cards=[source_card()])
        if role is AgentRole.WRITER:
            return self._write(task, context)
        if role is AgentRole.REVIEWER:
            sections = [
                state.model_copy(update={"status": SectionStatus.REVIEWED}, deep=True)
                for state in context.sections
                if state.version > 0
            ]
            return AgentResult(entry=self._entry(task, context), sections=sections)
        if role is AgentRole.VALIDATOR:
            report = self._reports[context.iteration]
            return AgentResult(entry=self._entry(task, context), report=report)
        return AgentResult(entry=self._entry(task, context))

    def _write(self, task: Task, context: TaskContext) -> AgentResult:
        state = context.sections[0]
        patch_id = sample_ulid(self.next_seed())
        if state.version == 0:
            ops = [PatchOp(op=PatchOpType.REPLACE_SECTION, text=FIRST_DRAFT)]
        else:
            ops = [
                PatchOp(
                    op=PatchOpType.INSERT_AFTER,
                    anchor="honest.",
                    text=f" Iteration {context.iteration} tightens the argument.",
                )
            ]
        patch = Patch(
            section_id="introduction", base_version=state.version, ops=ops, patch_id=patch_id
        )
        return AgentResult(
            entry=self._entry(task, context, patch_ref=patch_ref_for(patch_id)),
            patches=[patch],
        )

    def _entry(
        self, task: Task, context: TaskContext, *, patch_ref: str | None = None
    ) -> ChangeEntry:
        return entry_for(
            task,
            seed=self.next_seed(),
            iteration=context.iteration,
            tokens=self._tokens,
            patch_ref=patch_ref,
            summary=f"{task.role.value} completed iteration {context.iteration}.",
        )


def build(
    rubric: Rubric,
    *,
    planner: Any,
    executor: Any,
    policies: Policies | None = None,
    **kwargs: Any,
) -> Orchestrator:
    return Orchestrator(
        planner=planner,
        executors=executor,
        article=ArticleStore(),
        memory=MemoryStore(policies or Policies()),
        rubric=rubric,
        policies=policies or Policies(),
        **kwargs,
    )


# --------------------------------------------------------------------------- happy paths
async def test_one_iteration_happy_path_offers_finish(rubric: Rubric) -> None:
    executor = FakeExecutor()
    planner = FakePlanner([make_plan()])
    orchestrator = build(rubric, planner=planner, executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.OFFER_FINISH
    assert outcome.phase is RunPhase.DONE
    assert outcome.iterations == 1
    assert outcome.q_history == (85,)
    assert outcome.final_decision.action is LoopAction.OFFER_FINISH
    assert outcome.planner_tokens == planner.tokens_spent > 0
    assert outcome.run_tokens == outcome.agent_tokens + outcome.planner_tokens
    assert outcome.best_q == 85.0 and outcome.best_snapshot_id is not None
    assert estimate_tokens(outcome.digest_text) <= 2_000

    # The article actually changed, the citation site resolved by text only,
    # the reviewer promotion landed, and the card reached the source store.
    assert await orchestrator.article.get_text("introduction") == FIRST_DRAFT
    states = {state.id: state for state in outcome.sections}
    assert states["introduction"].status is SectionStatus.REVIEWED
    assert states["introduction"].version == 1
    assert orchestrator.memory.sources.get(source_card().id) is not None
    entries = orchestrator.memory.ledger.entries()
    assert [entry.role for entry in entries] == list(CHAIN)
    assert all(entry.status is ChangeStatus.OK for entry in entries)
    assert executor.calls == list(CHAIN)


async def test_two_iterations_replan_from_the_digest(rubric: Rubric) -> None:
    reports = {
        1: make_report(q=50, iteration=1, failed=("total_words_within_tolerance",)),
        2: make_report(q=85, iteration=2),
    }
    executor = FakeExecutor(reports=reports)
    planner = FakePlanner(
        [make_plan(version=1, seed_base=100), make_plan(version=2, seed_base=200)]
    )
    orchestrator = build(rubric, planner=planner, executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.OFFER_FINISH
    assert outcome.iterations == 2
    assert outcome.q_history == (50, 85)
    assert outcome.final_plan_version == 2
    assert [d.action for d in outcome.decisions] == [LoopAction.CONTINUE, LoopAction.OFFER_FINISH]
    assert "quality_below_minimum" in outcome.decisions[0].reasons
    assert planner.replan_digests and planner.replan_digests[0].strip()
    text = await orchestrator.article.get_text("introduction")
    assert "Iteration 2 tightens the argument." in text
    assert len(await orchestrator.article.snapshots()) == 2


async def test_end_to_end_with_the_real_planner_over_mock_provider(rubric: Rubric) -> None:
    plan = make_plan()
    verdict = Verdict(meets_minimum=True, recommendation="finish")
    script = (
        Script()
        .reply("planner", 0, plan.model_dump_json())
        .reply("planner", 1, verdict.model_dump_json())
    )
    provider = MockProvider(script)
    orchestrator = build(rubric, planner=LLMPlanner(provider), executor=FakeExecutor())

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.OFFER_FINISH
    assert provider.call_count == 2  # one plan, one evaluation - nothing else
    assert outcome.planner_tokens == provider.total_tokens().total
    assert await orchestrator.article.get_text("introduction") == FIRST_DRAFT


# --------------------------------------------------------------------------- stopping paths
async def test_needs_user_input_pauses_and_blocks_dependents(rubric: Rubric) -> None:
    def researcher(task: Task, context: TaskContext) -> AgentResult:
        _ = context
        return AgentResult(
            entry=entry_for(
                task,
                seed=777,
                status=ChangeStatus.NEEDS_USER_INPUT,
                summary="No verifiable source supports the pilot claim; user input needed.",
            )
        )

    executor = FakeExecutor(overrides={AgentRole.RESEARCHER: researcher})
    orchestrator = build(rubric, planner=FakePlanner([make_plan()]), executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.AWAITING_USER
    assert outcome.phase is RunPhase.PAUSED
    assert outcome.final_decision.action is LoopAction.ASK_USER
    assert outcome.questions and "source" in outcome.questions[0]
    assert executor.calls == [AgentRole.ARCHITECT, AgentRole.RESEARCHER]  # writer never ran
    assert await orchestrator.article.get_text("introduction") == ""


async def test_error_streak_stops_the_run(rubric: Rubric) -> None:
    architect = make_task(100, AgentRole.ARCHITECT)
    researcher = make_task(101, AgentRole.RESEARCHER)
    writer = make_task(102, AgentRole.WRITER)
    validator = make_task(
        103, AgentRole.VALIDATOR, depends_on=(architect.id, researcher.id, writer.id)
    )
    plan = Plan(version=1, goal="Fail fast.", tasks=[architect, researcher, writer, validator])

    def boom(task: Task, context: TaskContext) -> AgentResult:
        _ = (task, context)
        raise ProviderTimeout("injected timeout", provider="mock")

    executor = FakeExecutor(overrides=dict.fromkeys(CHAIN, boom))
    orchestrator = build(rubric, planner=FakePlanner([plan]), executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_ERRORS
    assert outcome.final_decision.action is LoopAction.STOP_ERRORS
    entries = orchestrator.memory.ledger.entries()
    assert len(entries) == 3  # the dependent validator never became ready
    assert all(entry.status is ChangeStatus.FAILED for entry in entries)
    assert "provider_timeout" in entries[0].summary


async def test_run_token_cap_stops_the_run(rubric: Rubric) -> None:
    policies = Policies(run_token_cap=10_000, iteration_token_cap=10_000, call_input_cap=8_000)
    reports = {1: make_report(q=40, iteration=1, failed=("no_placeholders",))}
    executor = FakeExecutor(reports=reports, entry_tokens=usage(3_000, 0))
    orchestrator = build(
        rubric, planner=FakePlanner([make_plan()]), executor=executor, policies=policies
    )

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_BUDGET
    assert "run_token_cap_reached" in outcome.final_decision.reasons
    assert outcome.run_tokens >= policies.run_token_cap


async def test_user_stop_before_the_run(rubric: Rubric) -> None:
    orchestrator = build(rubric, planner=FakePlanner([make_plan()]), executor=FakeExecutor())
    orchestrator.request_stop()

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_USER
    assert outcome.final_decision.action is LoopAction.STOP_USER
    assert orchestrator.memory.ledger.entries() == []


async def test_regression_erodes_and_rolls_back_to_the_best_snapshot(rubric: Rubric) -> None:
    reports = {
        1: make_report(q=80, iteration=1, failed=("abstract_within_range",)),
        2: make_report(q=55, iteration=2, failed=("abstract_within_range",)),
    }
    executor = FakeExecutor(reports=reports)
    planner = FakePlanner(
        [make_plan(version=1, seed_base=100), make_plan(version=2, seed_base=200)]
    )
    orchestrator = build(rubric, planner=planner, executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_EROSION
    assert "quality_regression_detected" in outcome.final_decision.reasons
    assert outcome.rolled_back is True
    assert outcome.best_q == 80.0
    # The iteration-2 edit is gone: the article is the iteration-1 body again.
    assert await orchestrator.article.get_text("introduction") == FIRST_DRAFT


async def test_replan_failure_ends_the_run_with_a_decision(rubric: Rubric) -> None:
    reports = {1: make_report(q=40, iteration=1, failed=("no_placeholders",))}
    planner = FakePlanner(
        [make_plan()], replan_error=ProviderTimeout("planner down", provider="anthropic")
    )
    orchestrator = build(rubric, planner=planner, executor=FakeExecutor(reports=reports))

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_ERRORS
    assert outcome.final_decision.reasons == ["planner_failed"]
    assert outcome.final_decision.evidence["error_code"] == "provider_timeout"
    assert len(outcome.decisions) == 2  # CONTINUE, then the planner-failure stop


async def test_initial_planner_failure_is_a_clean_stop(rubric: Rubric) -> None:
    planner = FakePlanner([ProviderTimeout("planner down", provider="anthropic")])
    orchestrator = build(rubric, planner=planner, executor=FakeExecutor())

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_ERRORS
    assert outcome.iterations == 0
    assert outcome.q_history == ()


# --------------------------------------------------------------------------- trust checks
async def test_stale_patch_is_recorded_failed_and_never_applied(rubric: Rubric) -> None:
    def stale_writer(task: Task, context: TaskContext) -> AgentResult:
        _ = context
        patch_id = sample_ulid(4242)
        patch = Patch(
            section_id="introduction",
            base_version=9,  # the head is version 0
            ops=[PatchOp(op=PatchOpType.REPLACE_SECTION, text="Never lands.")],
            patch_id=patch_id,
        )
        return AgentResult(
            entry=entry_for(task, seed=4243, patch_ref=patch_ref_for(patch_id)), patches=[patch]
        )

    policies = Policies(max_iterations=1)
    executor = FakeExecutor(overrides={AgentRole.WRITER: stale_writer})
    orchestrator = build(
        rubric, planner=FakePlanner([make_plan()]), executor=executor, policies=policies
    )

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.STOPPED_BUDGET  # the one allowed iteration ran out
    assert await orchestrator.article.get_text("introduction") == ""
    writer_entries = orchestrator.memory.ledger.by_role(AgentRole.WRITER)
    assert len(writer_entries) == 1
    assert writer_entries[0].status is ChangeStatus.FAILED
    assert "rejected" in writer_entries[0].summary


async def test_result_over_its_task_budget_is_rejected(rubric: Rubric) -> None:
    policies = Policies(max_iterations=1)
    executor = FakeExecutor(entry_tokens=usage(6_000, 0))  # task budgets are 5_000
    orchestrator = build(
        rubric, planner=FakePlanner([make_plan()]), executor=executor, policies=policies
    )

    await orchestrator.run(make_brief())

    assert await orchestrator.article.get_text("introduction") == ""
    entries = orchestrator.memory.ledger.entries()
    assert entries and all(e.status is ChangeStatus.FAILED for e in entries)
    assert "task budget" in entries[0].summary


async def test_result_that_does_not_own_the_task_is_discarded(rubric: Rubric) -> None:
    def confused_writer(task: Task, context: TaskContext) -> AgentResult:
        _ = context
        patch_id = sample_ulid(555)
        # The entry claims the researcher did the writer's task.
        return AgentResult(
            entry=entry_for(
                task, seed=556, role=AgentRole.RESEARCHER, patch_ref=patch_ref_for(patch_id)
            ),
            patches=[
                Patch(
                    section_id="introduction",
                    base_version=0,
                    ops=[PatchOp(op=PatchOpType.REPLACE_SECTION, text="Smuggled text.")],
                    patch_id=patch_id,
                )
            ],
        )

    policies = Policies(max_iterations=1)
    executor = FakeExecutor(overrides={AgentRole.WRITER: confused_writer})
    orchestrator = build(
        rubric, planner=FakePlanner([make_plan()]), executor=executor, policies=policies
    )

    await orchestrator.run(make_brief())

    assert await orchestrator.article.get_text("introduction") == ""
    writer_entries = orchestrator.memory.ledger.by_role(AgentRole.WRITER)
    assert writer_entries[0].status is ChangeStatus.FAILED
    assert "does not own" in writer_entries[0].summary


async def test_missing_executor_becomes_a_failed_entry_not_a_crash(rubric: Rubric) -> None:
    policies = Policies(max_iterations=1)
    executor = FakeExecutor()
    orchestrator = Orchestrator(
        planner=FakePlanner([make_plan()]),
        executors={role: executor for role in CHAIN if role is not AgentRole.VALIDATOR},
        rubric=rubric,
        policies=policies,
    )

    outcome = await orchestrator.run(make_brief())

    validator_entries = orchestrator.memory.ledger.by_role(AgentRole.VALIDATOR)
    assert validator_entries[0].status is ChangeStatus.FAILED
    assert "No executor" in validator_entries[0].summary
    assert outcome.q_history == ()  # nobody scored the article


async def test_researcher_cannot_smuggle_a_verified_card(rubric: Rubric) -> None:
    def overclaiming_researcher(task: Task, context: TaskContext) -> AgentResult:
        _ = context
        card = source_card(901).model_copy(
            update={"verification": VerificationStatus.VERIFIED}, deep=True
        )
        return AgentResult(entry=entry_for(task, seed=902), source_cards=[card])

    policies = Policies(max_iterations=1)
    executor = FakeExecutor(overrides={AgentRole.RESEARCHER: overclaiming_researcher})
    orchestrator = build(
        rubric, planner=FakePlanner([make_plan()]), executor=executor, policies=policies
    )

    await orchestrator.run(make_brief())

    assert orchestrator.memory.sources.get(sample_ulid(901)) is None
    researcher_entries = orchestrator.memory.ledger.by_role(AgentRole.RESEARCHER)
    assert researcher_entries[0].status is ChangeStatus.FAILED


# --------------------------------------------------------------------------- scheduling
async def test_max_parallel_bounds_concurrent_execution(rubric: Rubric) -> None:
    architect = make_task(100, AgentRole.ARCHITECT)
    researcher = make_task(101, AgentRole.RESEARCHER)
    writer = make_task(102, AgentRole.WRITER)
    validator = make_task(
        103, AgentRole.VALIDATOR, depends_on=(architect.id, researcher.id, writer.id)
    )
    plan = Plan(version=1, goal="Parallel wave.", tasks=[architect, researcher, writer, validator])
    executor = FakeExecutor(delay=0.02)
    orchestrator = build(
        rubric,
        planner=FakePlanner([plan]),
        executor=executor,
        policies=Policies(max_parallel=2),
    )

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.OFFER_FINISH
    assert executor.max_active == 2  # bounded by the policy, and actually parallel


async def test_an_orchestrator_runs_exactly_once(rubric: Rubric) -> None:
    orchestrator = build(rubric, planner=FakePlanner([make_plan()]), executor=FakeExecutor())
    await orchestrator.run(make_brief())
    with pytest.raises(InvalidRunStateError):
        await orchestrator.run(make_brief())


async def test_duplicate_entry_ids_are_re_stamped_before_the_ledger(rubric: Rubric) -> None:
    # Two independent tasks whose (buggy) executors reuse one entry id: the
    # ledger is append-only and keyed by id, so the orchestrator re-stamps.
    architect = make_task(100, AgentRole.ARCHITECT)
    researcher = make_task(101, AgentRole.RESEARCHER)
    writer = make_task(102, AgentRole.WRITER)
    validator = make_task(
        103, AgentRole.VALIDATOR, depends_on=(architect.id, researcher.id, writer.id)
    )
    plan = Plan(version=1, goal="Duplicate ids.", tasks=[architect, researcher, writer, validator])

    def same_entry_id(task: Task, context: TaskContext) -> AgentResult:
        _ = context
        return AgentResult(entry=entry_for(task, seed=31337))

    executor = FakeExecutor(
        overrides={AgentRole.ARCHITECT: same_entry_id, AgentRole.RESEARCHER: same_entry_id}
    )
    orchestrator = build(rubric, planner=FakePlanner([plan]), executor=executor)

    outcome = await orchestrator.run(make_brief())

    assert outcome.status is RunStatus.OFFER_FINISH
    entry_ids = [entry.id for entry in orchestrator.memory.ledger.entries()]
    assert len(entry_ids) == len(set(entry_ids)) == 4
