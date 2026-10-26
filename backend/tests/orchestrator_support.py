"""Scripted planner/agent doubles for the Step 9 test-suite.

Everything is deterministic and offline (HARD RULE 5): the planner double
returns hand-built ``Plan`` objects, the agent double returns hand-built
``AgentResult`` objects with scripted validator Q values. No MockProvider
call is needed because these doubles sit exactly on the orchestrator's
ports (``PlannerPort`` / ``AgentExecutor``); provider-level behaviour is
covered by the Step 5/8 suites.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.context import RunState
from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, ChangeStatus, PatchOpType, Recommendation
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.ids import new_ulid
from app.planner.budget import PlanBudget
from app.planner.probes import ArticleView

__all__ = [
    "BRIEF_PAYLOAD",
    "CrashingAgents",
    "ScriptedAgents",
    "ScriptedPlanner",
    "brief",
    "make_entry",
    "make_report",
    "make_task",
]

BRIEF_PAYLOAD: dict[str, object] = {
    "topic": "Token-frugal orchestration for agentic academic writing",
    "article_type": "research",
    "audience": "graduate students and applied NLP practitioners",
    "language": "en",
    "target_words": 4000,
    "citation_style": "APA7",
    "must_include": [],
    "must_avoid": [],
    "provided_data": [],
}


def brief() -> Brief:
    """A valid brief for orchestrator tests."""
    return Brief.model_validate(BRIEF_PAYLOAD)


def make_task(
    role: AgentRole,
    title: str = "Do the work",
    *,
    section_ids: Sequence[str] = ("introduction",),
    depends_on: Sequence[str] = (),
    task_id: str | None = None,
) -> Task:
    """One minimal valid task."""
    return Task(
        id=task_id or new_ulid(),
        title=title,
        role=role,
        section_ids=list(section_ids),
        instructions="Execute the scripted behaviour.",
        acceptance=["A scripted result is returned"],
        token_budget=2_000,
        depends_on=list(depends_on),
    )


def make_report(
    iteration: int,
    q: int,
    *,
    gates_pass: bool = False,
    ratio: float = 1.0,
    failed_gate: str | None = None,
) -> QualityReport:
    """A validator report; all gates share one verdict unless overridden."""
    gates = dict.fromkeys(GATE_NAMES, gates_pass)
    if failed_gate is not None:
        gates = dict.fromkeys(GATE_NAMES, True)
        gates[failed_gate] = False
    return QualityReport(
        iter=iteration, gates=gates, scores={}, Q=q, verified_citation_ratio=ratio, issues=[]
    )


def make_entry(
    *,
    iteration: int = 1,
    task_id: str | None = None,
    role: AgentRole = AgentRole.WRITER,
    status: ChangeStatus = ChangeStatus.OK,
    tokens: int = 0,
    opened: Sequence[str] = (),
    closed: Sequence[str] = (),
) -> ChangeEntry:
    """One ledger entry with the knobs loop-controller tests need."""
    return ChangeEntry(
        id=new_ulid(),
        iter=iteration,
        task_id=task_id or new_ulid(),
        role=role,
        targets=["introduction"],
        summary="Scripted entry.",
        tokens=TokenUsage(in_=tokens, out=0, cached=0),
        issues_opened=list(opened),
        issues_closed=list(closed),
        status=status,
    )


class ScriptedPlanner:
    """Planner double: writer->validator pairs, verdict by report Q/gates."""

    def __init__(
        self,
        *,
        verdict: Verdict | None = None,
        plan_failures: int = 0,
        usage_per_call: int = 0,
    ) -> None:
        self._verdict = verdict or Verdict(
            meets_minimum=True, missing=[], risks=[], recommendation=Recommendation.FINISH
        )
        self._plan_failures = plan_failures
        self._usage_per_call = usage_per_call
        self._usage = TokenUsage(in_=0, out=0, cached=0)
        self.plan_calls = 0
        self.replan_calls: list[bool] = []
        self.evaluate_calls = 0

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    def _spend(self) -> None:
        self._usage = TokenUsage(
            in_=self._usage.in_ + self._usage_per_call, out=self._usage.out, cached=0
        )

    async def plan(self, *, brief: Brief, digest: str, budget: PlanBudget) -> Plan:
        del brief, digest, budget
        self.plan_calls += 1
        self._spend()
        if self._plan_failures > 0:
            self._plan_failures -= 1
            raise RuntimeError("scripted planner outage")
        writer = make_task(AgentRole.WRITER, "Draft the introduction")
        validator = make_task(AgentRole.VALIDATOR, "Validate", depends_on=[writer.id])
        return Plan(version=1, goal="A scripted article.", tasks=[writer, validator])

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
        del brief, digest, report, decision, budget
        self.replan_calls.append(strategy_change)
        self._spend()
        writer = make_task(AgentRole.WRITER, "Revise the introduction")
        validator = make_task(AgentRole.VALIDATOR, "Revalidate", depends_on=[writer.id])
        return Plan(
            version=current.version + 1,
            goal=current.goal,
            tasks=[*current.tasks, writer, validator],
        )

    async def evaluate(
        self,
        *,
        brief: Brief,
        digest: str,
        report: QualityReport | None,
        view: ArticleView,
        iteration: int,
    ) -> Verdict:
        del brief, digest, report, view, iteration
        self.evaluate_calls += 1
        self._spend()
        return self._verdict


class ScriptedAgents:
    """Agent double: writer patches, validator reports scripted Q values."""

    def __init__(
        self,
        q_series: Sequence[int] = (85,),
        *,
        gates_pass_from_q: int = 80,
        tokens_per_entry: int = 0,
        writer_status: ChangeStatus = ChangeStatus.OK,
        failures_before_success: int = 0,
    ) -> None:
        self._q_series = list(q_series) or [85]
        self._gates_pass_from_q = gates_pass_from_q
        self._tokens = tokens_per_entry
        self._writer_status = writer_status
        self._failures = failures_before_success
        self._validations = 0
        self.executed: list[str] = []

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        from app.providers.errors import ProviderTimeout

        self.executed.append(task.id)
        if self._failures > 0:
            self._failures -= 1
            raise ProviderTimeout("scripted timeout", provider="mock")
        patches: list[Patch] = []
        report: QualityReport | None = None
        patch_ref: str | None = None
        status = ChangeStatus.OK
        if task.role is AgentRole.WRITER:
            status = self._writer_status
            if status is ChangeStatus.OK and task.section_ids:
                section_id = task.section_ids[0]
                section = next(
                    (state for state in run_state.sections if state.id == section_id), None
                )
                patch = Patch(
                    section_id=section_id,
                    base_version=section.version if section is not None else 0,
                    patch_id=new_ulid(),
                    ops=[
                        PatchOp(
                            op=PatchOpType.REPLACE_SECTION,
                            text=f"Scripted text, iteration {run_state.iter}.",
                        )
                    ],
                )
                patches = [patch]
                patch_ref = f"patch:{patch.patch_id}"
        elif task.role is AgentRole.VALIDATOR:
            q = self._q_series[min(self._validations, len(self._q_series) - 1)]
            self._validations += 1
            report = make_report(run_state.iter, q, gates_pass=q >= self._gates_pass_from_q)
        entry = ChangeEntry(
            id=new_ulid(),
            iter=run_state.iter,
            task_id=task.id,
            role=task.role,
            targets=list(task.section_ids),
            summary="Scripted agent result.",
            patch_ref=patch_ref,
            tokens=TokenUsage(in_=self._tokens, out=0, cached=0),
            status=status,
        )
        return AgentResult(entry=entry, patches=patches, report=report)


class _SimulatedCrash(BaseException):
    """A process-killing fault: not an Exception, so nothing retries it."""


class CrashingAgents:
    """Wraps ScriptedAgents; crashes the process on the first validator task."""

    def __init__(self, inner: ScriptedAgents) -> None:
        self.inner = inner
        self.crashed = False

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        if not self.crashed and task.role is AgentRole.VALIDATOR:
            self.crashed = True
            raise _SimulatedCrash
        return await self.inner.run(task, run_state)


CrashingAgents.Crash = _SimulatedCrash  # type: ignore[attr-defined]
