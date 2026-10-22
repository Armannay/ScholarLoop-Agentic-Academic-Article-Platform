"""The five governor pieces wired together, on MockProvider only.

This is the shape the orchestrator will use: build the prompt once, estimate it, ask the
budget for a reservation, make one mock call, settle the real usage and read the spend
back. No network, no key, no clock dependency (HARD RULES 3 and 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.agents.base import Governor
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, BindingRole, SectionStatus
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.hashing import content_hash
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.governor import (
    BudgetGovernor,
    ContextBuilder,
    RuleLibrary,
    TelemetryRecorder,
    TierResolver,
    TokenEstimator,
)
from app.providers.base import prompt_tokens
from app.providers.mock import MockProvider, Script
from app.skills.loader import SkillLoader
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.governor


@dataclass(frozen=True)
class RunState:
    brief: Brief | None = None
    iter: int = 1
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()
    digest: str = ""
    section_texts: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def run_state() -> RunState:
    text = "Token budgets turn a guess into a measurement. " * 12
    section = SectionState(
        id="introduction",
        title="Introduction",
        order=0,
        version=1,
        hash=content_hash(text),
        words=len(text.split()),
        status=SectionStatus.DRAFT,
    )
    card = SourceCard(
        id=sample_ulid(2),
        title="Token-frugal agents",
        authors=["Nguyen, A."],
        year=2024,
        venue="Journal of Agentic Systems",
        doi="10.1234/jas.2024.7",
        snippet="Hierarchical budgets cut spend by 38 percent without lowering scores.",
    )
    return RunState(
        brief=Brief.model_validate(SAMPLE_BRIEF),
        sections=(section,),
        sources=(card,),
        digest="- iter 1: architect outlined IMRaD sections",
        section_texts={"introduction": text},
    )


def make_task() -> Task:
    return Task(
        id=sample_ulid(1),
        title="Draft the introduction",
        role=AgentRole.WRITER,
        section_ids=["introduction"],
        instructions="Draft the introduction in IMRaD structure with APA citation.",
        acceptance=["Cites the source card"],
        token_budget=20_000,
    )


@pytest.fixture
def builder(repo_root: Path, rubric: Rubric) -> ContextBuilder:
    return ContextBuilder(
        rules=RuleLibrary(repo_root / "rules"),
        rubric=rubric,
        skills=SkillLoader(repo_root / "skills"),
        max_input_tokens=20_000,
        max_output_tokens=512,
    )


async def test_one_call_from_context_to_token_roi(
    builder: ContextBuilder, run_state: RunState, policies: Policies
):
    task = make_task()
    plan = builder.plan(task, run_state)
    provider = MockProvider(Script())
    estimator = TokenEstimator(provider=provider)
    governor = BudgetGovernor.for_run("run-integration", policies=policies)

    estimate = await estimator.estimate(plan.request)
    reservation = governor.precheck(
        estimate.to_budget(output_tokens=plan.request.max_output_tokens),
        role="writer",
        task=task,
    )
    response = await provider.generate(
        plan.request, model="mock-standard-1", role="writer", account_id=None
    )
    status = governor.record(response.usage, reservation=reservation)
    drift = estimator.reconcile(estimate, response.usage)

    telemetry = TelemetryRecorder(run_id="run-integration")
    telemetry.record(
        response, iter=1, task_id=task.id, role="writer", est_input_tokens=estimate.input_tokens
    )
    telemetry.record_quality(1, 61.0)
    telemetry.record_quality(2, 64.0)
    telemetry.record(
        response, iter=2, task_id=task.id, role="reviewer", est_input_tokens=estimate.input_tokens
    )

    assert governor.scope.run_id == "run-integration"
    assert status.run.used == response.usage.total
    assert status.iteration.used == response.usage.total
    assert estimate.input_tokens >= prompt_tokens(plan.request)
    assert drift.actual == response.usage.in_ + response.usage.cached
    assert telemetry.totals().calls == 2
    assert telemetry.tokens_by_role()["writer"].tokens == response.usage.total
    assert telemetry.cache_hit_ratio() >= 0.0
    assert telemetry.summary().roi is not None


def test_budget_governor_satisfies_the_agent_runtime_seam(
    builder: ContextBuilder, run_state: RunState, policies: Policies
):
    governor = BudgetGovernor.for_run("run-seam", policies=policies)
    request = builder.build(make_task(), run_state)

    assert isinstance(governor, Governor)
    reservation = governor.precheck(request, role=BindingRole.WRITER, task=make_task())
    assert reservation.is_open


def test_tier_resolution_picks_the_model_the_binding_asks_for(policies: Policies):
    resolver = TierResolver(policies)

    choice = resolver.resolve("compactor", provider="anthropic")

    assert choice.tier.value == "cheap"
    assert choice.model


def test_no_live_provider_is_reachable_from_the_governor() -> None:
    """HARD RULE 5: the governor package never imports an HTTP client."""
    import app.governor as governor_package

    source_root = Path(governor_package.__file__).parent
    forbidden = ("httpx", "requests", "aiohttp", "urllib3")
    for module in source_root.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        imports: list[str] = [
            line for line in text.splitlines() if line.startswith(("import ", "from "))
        ]
        assert not any(bad in line for line in imports for bad in forbidden), (
            f"{module.name} imports a live HTTP client"
        )


def test_governor_sends_nothing_that_looks_like_a_key(
    builder: ContextBuilder, run_state: RunState
) -> None:
    """The prompt carries rules, references and task text - never credential material."""
    plan = builder.plan(make_task(), run_state)
    prompt: str = plan.request.model_dump_json()

    for marker in ("sk-ant", "sk-proj", "Bearer ", "api_key="):
        assert marker not in prompt
