"""Scripted end-to-end loop simulations (Step 9a, item 7).

Runs six Q-series scenarios through the *full* orchestrator - intake, state
machine, scheduler, article store, ledger, loop controller, checkpoints -
with scripted planner/agent doubles at the orchestrator's ports. No provider
is constructed and no token is spent: the doubles return contract objects
directly (zero-token entries, except the budget-burn scenario whose entries
carry scripted token counts so the cap detector has something to measure).

Scenarios: ``improving`` (completion checkpoint -> user finishes),
``plateau``, ``oscillating``, ``regressing`` (all stop on erosion via the
two-strike rule), ``budget_burn`` (``STOP_BUDGET``) and ``flaky_agent``
(provider timeouts absorbed by scheduler retries, then completion).

Usage::

    python -m app.scripts.simulate_loop [--scenario NAME]

Exit code 0 means every scenario reached its expected end state.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.agents.context import RunState
from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.enums import (
    AgentRole,
    ArticleType,
    ChangeStatus,
    CitationStyle,
    PatchOpType,
    Recommendation,
)
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.ids import new_ulid
from app.core.policies import Policies
from app.orchestrator.persistence import InMemoryRunStore, RunRecord
from app.orchestrator.runner import Orchestrator, OrchestratorConfig
from app.orchestrator.state_machine import RunStateName
from app.orchestrator.stop import StopOption, UserDecision
from app.planner.budget import PlanBudget
from app.planner.probes import ArticleView
from app.providers.errors import ProviderTimeout

__all__ = ["SCENARIOS", "Scenario", "main", "run_scenario"]


def _task(role: AgentRole, title: str, depends_on: Sequence[str] = ()) -> Task:
    return Task(
        id=new_ulid(),
        title=title,
        role=role,
        section_ids=["introduction"],
        instructions="Scripted simulation task.",
        acceptance=["Scripted result returned"],
        token_budget=1_000,
        depends_on=list(depends_on),
    )


class ScriptedPlanner:
    """Plans writer->validator pairs; evaluates by the report's Q."""

    def __init__(self, q_min: int) -> None:
        self._q_min = q_min
        self._usage = TokenUsage(in_=0, out=0, cached=0)

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    async def plan(self, *, brief: Brief, digest: str, budget: PlanBudget) -> Plan:
        del brief, digest, budget
        writer = _task(AgentRole.WRITER, "Draft the introduction")
        validator = _task(AgentRole.VALIDATOR, "Validate the article", [writer.id])
        return Plan(
            version=1,
            goal="A scripted article at target quality.",
            tasks=[writer, validator],
        )

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
        del brief, digest, report, decision, strategy_change, budget
        writer = _task(AgentRole.WRITER, "Revise the introduction")
        validator = _task(AgentRole.VALIDATOR, "Revalidate the article", [writer.id])
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
        del brief, digest, view, iteration
        if report is not None and self._q_min <= report.Q and report.all_gates_pass:
            return Verdict(
                meets_minimum=True,
                missing=[],
                risks=[],
                recommendation=Recommendation.FINISH,
            )
        return Verdict(
            meets_minimum=False,
            missing=["Quality below the scripted bar"],
            risks=[],
            recommendation=Recommendation.CONTINUE,
        )


class ScriptedAgents:
    """Writer patches the introduction; validator reports the scripted Q."""

    def __init__(
        self,
        q_series: Sequence[int],
        *,
        tokens_per_entry: int = 0,
        writer_failures: int = 0,
        gates_pass_from_q: int = 80,
    ) -> None:
        self._q_series = list(q_series)
        self._tokens = tokens_per_entry
        self._writer_failures = writer_failures
        self._gates_pass_from_q = gates_pass_from_q
        self._validations = 0

    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        if task.role is AgentRole.WRITER and self._writer_failures > 0:
            self._writer_failures -= 1
            raise ProviderTimeout("scripted provider timeout", provider="mock")
        patches: list[Patch] = []
        report: QualityReport | None = None
        patch_ref: str | None = None
        if task.role is AgentRole.WRITER:
            section = next(
                (state for state in run_state.sections if state.id == "introduction"), None
            )
            patch = Patch(
                section_id="introduction",
                base_version=section.version if section is not None else 0,
                patch_id=new_ulid(),
                ops=[
                    PatchOp(
                        op=PatchOpType.REPLACE_SECTION,
                        text=f"Scripted draft, iteration {run_state.iter}.",
                    )
                ],
            )
            patches = [patch]
            patch_ref = f"patch:{patch.patch_id}"
        elif task.role is AgentRole.VALIDATOR:
            index = min(self._validations, len(self._q_series) - 1)
            self._validations += 1
            q = self._q_series[index]
            report = QualityReport(
                iter=run_state.iter,
                gates=dict.fromkeys(GATE_NAMES, q >= self._gates_pass_from_q),
                scores={},
                Q=q,
                verified_citation_ratio=1.0,
                issues=[],
            )
        entry = ChangeEntry(
            id=new_ulid(),
            iter=run_state.iter,
            task_id=task.id,
            role=task.role,
            targets=["introduction"],
            summary="Scripted simulation result.",
            patch_ref=patch_ref,
            tokens=TokenUsage(in_=self._tokens, out=0, cached=0),
            status=ChangeStatus.OK,
        )
        return AgentResult(entry=entry, patches=patches, report=report)


@dataclass(frozen=True)
class Scenario:
    """One scripted run and the end state it must reach."""

    name: str
    q_series: tuple[int, ...]
    expected_state: RunStateName
    tokens_per_entry: int = 0
    writer_failures: int = 0
    finish_at_checkpoint: bool = False


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("improving", (50, 65, 85), RunStateName.DONE, finish_at_checkpoint=True),
    Scenario("plateau", (50, 50, 50, 50, 50), RunStateName.STOPPED_EROSION),
    Scenario("oscillating", (60, 70, 60, 70, 60, 70), RunStateName.STOPPED_EROSION),
    Scenario("regressing", (75, 65, 55, 45), RunStateName.STOPPED_EROSION),
    Scenario(
        "budget_burn", (50, 55, 60, 65), RunStateName.STOPPED_BUDGET, tokens_per_entry=300_000
    ),
    Scenario(
        "flaky_agent",
        (55, 85),
        RunStateName.DONE,
        writer_failures=2,
        finish_at_checkpoint=True,
    ),
)

_BRIEF = Brief(
    topic="Scripted simulation of the ScholarLoop control loop",
    article_type=ArticleType.RESEARCH,
    audience="graduate students",
    language="en",
    target_words=4000,
    citation_style=CitationStyle.APA7,
)


async def run_scenario(scenario: Scenario, *, policies: Policies | None = None) -> RunRecord:
    """Drive one scenario through a fresh orchestrator (in-memory store)."""
    resolved = policies or Policies()
    orchestrator = Orchestrator(
        ScriptedPlanner(resolved.q_min),
        ScriptedAgents(
            scenario.q_series,
            tokens_per_entry=scenario.tokens_per_entry,
            writer_failures=scenario.writer_failures,
        ),
        policies=resolved,
        store=InMemoryRunStore(),
        config=OrchestratorConfig(backoff_base_s=0.0, planner_backoff_s=0.0),
    )
    record = orchestrator.create_run(f"simulate-{scenario.name}", _BRIEF)
    record = await orchestrator.run(record.id)
    if scenario.finish_at_checkpoint and record.state is RunStateName.CHECKPOINT:
        record = await orchestrator.apply_decision(
            record.id, UserDecision(option=StopOption.FINISH)
        )
    return record


async def _run_all(selected: str | None) -> int:
    failures = 0
    for scenario in SCENARIOS:
        if selected is not None and scenario.name != selected:
            continue
        record = await run_scenario(scenario)
        ok = record.state is scenario.expected_state
        failures += 0 if ok else 1
        actions = " -> ".join(decision.action.value for decision in record.decisions)
        print(
            f"[{'ok' if ok else 'FAIL'}] {scenario.name:<14} state={record.state.value:<16} "
            f"iters={record.iter} Q={record.q_series()} tokens={record.total_tokens_spent()} "
            f"decisions: {actions}"
        )
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: run the scenarios, print a table, exit non-zero on drift."""
    parser = argparse.ArgumentParser(description="Scripted ScholarLoop loop simulations.")
    parser.add_argument("--scenario", choices=[s.name for s in SCENARIOS], default=None)
    args: Any = parser.parse_args(list(argv) if argv is not None else None)
    failures = asyncio.run(_run_all(args.scenario))
    if failures:
        print(f"{failures} scenario(s) ended in an unexpected state")
        return 1
    print("all scenarios terminated as expected (zero provider tokens spent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
