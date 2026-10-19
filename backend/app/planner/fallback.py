"""Deterministic planner fallbacks - a template plan and a report-only verdict.

Explicit opt-in, never a silent substitute: when a Planner model call fails the
Planner raises, and the *orchestrator* decides whether a degraded run on the
template is better than stopping. What this module guarantees is that the
template is always a valid, role-complete :class:`~app.contracts.plan.Plan`
derived from the brief and rubric alone - no invented sources (the researcher
task says so), no article prose, budgets summing exactly to the caller's cap.

The same helpers give tests and local bootstrapping a MockProvider-free baseline,
and ``verdict_from_report`` is the deterministic floor the model verdict is
checked against by the guardrails in :mod:`app.planner.validation`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, Recommendation
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.rubric import Rubric, SectionSpec
from app.planner.context import ABSTRACT_SECTION_ID
from app.planner.errors import PlannerError

__all__ = ["build_fallback_plan", "verdict_from_report"]

#: Smallest budget a template task may receive; below this no agent can do work.
MIN_TASK_BUDGET: int = 1_000


def build_fallback_plan(
    brief: Brief,
    rubric: Rubric,
    *,
    id_factory: Callable[[], str],
    budget_cap: int,
    version: int = 1,
) -> Plan:
    """A rubric-shaped plan: architect -> researcher -> writers -> reviewer -> validator.

    One writer task per prose section (``references`` carries no prose and is
    skipped; the reviewer/validator cover it), an abstract writer after the body
    writers, and budgets split by fixed weights so the sum lands exactly on
    ``budget_cap``.
    """
    spec = rubric.spec_for(brief.article_type)
    section_ids = [section.id for section in spec.sections]
    prose_sections = [section for section in spec.sections if section.accepts_prose]
    task_count = 4 + len(prose_sections) + (1 if spec.abstract.required else 0)
    if budget_cap < task_count * MIN_TASK_BUDGET:
        raise PlannerError(
            f"budget_cap {budget_cap} cannot fund a {task_count}-task fallback plan "
            f"(needs >= {task_count * MIN_TASK_BUDGET})",
            budget_cap=budget_cap,
            task_count=task_count,
        )

    architect_id, researcher_id, reviewer_id, validator_id = (id_factory() for _ in range(4))
    writer_ids = {section.id: id_factory() for section in prose_sections}
    abstract_id = id_factory() if spec.abstract.required else None

    style = brief.citation_style.value
    all_writer_ids = [writer_ids[section.id] for section in prose_sections]
    reviewer_depends = [*all_writer_ids, *([abstract_id] if abstract_id else [])]

    specs: list[tuple[int, Task]] = [
        (
            5,
            Task(
                id=architect_id,
                title="Outline the article structure",
                role=AgentRole.ARCHITECT,
                section_ids=section_ids,
                instructions=(
                    "Create the section outline for the rubric sections of this article type, "
                    "with the argument each section must carry and its target word range."
                ),
                acceptance=[
                    "Every rubric section has an outline entry with a word target",
                    "Section order matches the rubric order",
                ],
                token_budget=MIN_TASK_BUDGET,
                depends_on=[],
            ),
        ),
        (
            20,
            Task(
                id=researcher_id,
                title="Collect and card the sources",
                role=AgentRole.RESEARCHER,
                section_ids=section_ids,
                instructions=_researcher_instructions(brief),
                acceptance=[
                    "Every source card carries a DOI or URL and a snippet <= 60 words",
                    "Unverifiable claims are returned as needs_user_input, never invented",
                ],
                token_budget=MIN_TASK_BUDGET,
                depends_on=[architect_id],
            ),
        ),
    ]
    writer_weight_total = 45
    for section in prose_sections:
        specs.append(
            (
                _writer_weight(section, prose_sections, writer_weight_total),
                Task(
                    id=writer_ids[section.id],
                    title=f"Draft section: {section.title}",
                    role=AgentRole.WRITER,
                    section_ids=[section.id],
                    instructions=(
                        f"Draft the {section.title} section from the outline and the source "
                        "cards. Supported claims only; flag gaps as issues instead of hedging."
                    ),
                    acceptance=[
                        f"Section within {section.min_words}-{_upper(section)} words",
                        "Every non-obvious claim cites a source card id or provided data",
                        f"Citations follow {style}; no placeholders",
                    ],
                    token_budget=MIN_TASK_BUDGET,
                    depends_on=[researcher_id],
                ),
            )
        )
    if abstract_id is not None:
        specs.append(
            (
                5,
                Task(
                    id=abstract_id,
                    title="Write the abstract",
                    role=AgentRole.WRITER,
                    section_ids=[ABSTRACT_SECTION_ID],
                    instructions=(
                        "Write the abstract last, from the finished sections: problem, "
                        "approach, findings, significance."
                    ),
                    acceptance=[
                        f"Abstract within {spec.abstract.min_words}"
                        f"-{spec.abstract.max_words} words",
                        "No claim appears in the abstract that the body does not support",
                    ],
                    token_budget=MIN_TASK_BUDGET,
                    depends_on=all_writer_ids,
                ),
            )
        )
    specs.append(
        (
            12,
            Task(
                id=reviewer_id,
                title="Review the full draft",
                role=AgentRole.REVIEWER,
                section_ids=section_ids,
                instructions=(
                    "Review every section against its acceptance criteria: coherence, "
                    "supported claims, citation style, and brief must_include/must_avoid."
                ),
                acceptance=[
                    "Every must_include item is verifiably present",
                    "No must_avoid item appears anywhere in the draft",
                ],
                token_budget=MIN_TASK_BUDGET,
                depends_on=reviewer_depends,
            ),
        )
    )
    specs.append(
        (
            8,
            Task(
                id=validator_id,
                title="Validate gates and score quality",
                role=AgentRole.VALIDATOR,
                instructions=(
                    "Measure the article: report all 8 hard gates, score the rubric "
                    "criteria, verify citations both ways, and open an issue per defect."
                ),
                acceptance=[
                    "QualityReport carries all 8 gates and a weighted Q",
                    "Every unverified reference is flagged with an open issue",
                ],
                token_budget=MIN_TASK_BUDGET,
                depends_on=[reviewer_id],
            ),
        )
    )

    budgets = _allocate(budget_cap, [weight for weight, _ in specs])
    tasks = [
        task.model_copy(update={"token_budget": budget})
        for budget, (_, task) in zip(budgets, specs, strict=True)
    ]
    return Plan(version=version, goal=_fallback_goal(brief), tasks=tasks)


def verdict_from_report(report: QualityReport, *, q_min: int) -> Verdict:
    """The verdict the numbers alone support: gates, ``q_min`` and high issues."""
    missing: list[str] = [f"Hard gate failing: {name}" for name in report.failed_gates]
    if q_min > report.Q:
        missing.append(f"Q is {report.Q}, below the minimum {q_min}")
    missing.extend(
        f"High-severity issue open: {issue.id}" for issue in report.high_severity_issues()
    )
    meets = not missing
    return Verdict(
        meets_minimum=meets,
        missing=missing[:40],
        risks=[] if meets else ["Finishing now would ship an article below the quality bar."],
        recommendation=Recommendation.FINISH if meets else Recommendation.CONTINUE,
    )


# -- internals ---------------------------------------------------------------


def _writer_weight(section: SectionSpec, prose_sections: Sequence[SectionSpec], total: int) -> int:
    """Share of the writers' weight for one section, proportional to target words."""
    total_words = sum(item.target_words for item in prose_sections) or len(prose_sections)
    return max(1, round(total * section.target_words / total_words))


def _allocate(budget_cap: int, weights: Sequence[int]) -> list[int]:
    """Split ``budget_cap`` by weight; exact sum, every share >= MIN_TASK_BUDGET."""
    total_weight = sum(weights)
    shares = [max(MIN_TASK_BUDGET, budget_cap * weight // total_weight) for weight in weights]
    difference = budget_cap - sum(shares)
    # Deterministic adjustment: the heaviest task absorbs the remainder either way.
    heaviest = max(range(len(weights)), key=lambda index: weights[index])
    shares[heaviest] += difference
    if shares[heaviest] < MIN_TASK_BUDGET:  # pragma: no cover - caller pre-validates
        raise PlannerError("budget_cap too small for the fallback plan", budget_cap=budget_cap)
    return shares


def _researcher_instructions(brief: Brief) -> str:
    """Researcher instructions; provided data is the seed, never a licence to invent."""
    if brief.provided_data:
        return (
            f"Start from the {len(brief.provided_data)} user-provided data item(s) in the "
            "brief, then find real, locatable sources for the remaining claims. Card every "
            "source with a DOI or URL."
        )
    return (
        "Find real, locatable sources for the article's claims and card each one with a "
        "DOI or URL. If a needed source cannot be verified, return needs_user_input."
    )


def _fallback_goal(brief: Brief) -> str:
    """Honest provenance in the goal string, inside the contract's 800-char cap."""
    topic = " ".join(brief.topic.split())[:200]
    return (
        f"Fallback template plan (no Planner model output): a {brief.target_words}-word "
        f"{brief.article_type.value} article on {topic}, meeting all hard gates."
    )


def _upper(section: SectionSpec) -> str:
    """Word-range upper bound as acceptance text (``open`` when unbounded)."""
    return "open" if section.max_words is None else str(section.max_words)
