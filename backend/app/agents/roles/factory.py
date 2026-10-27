"""Wiring for the five role agents: one place that knows what each role needs.

A role agent has the same three required collaborators as any Step 5 agent (provider,
bindings, governor, context builder) plus at most four optional ones: a scoped tool
registry, a section text provider, a review scoreboard and a report source. Building them
one by one at every call site is how a reviewer ends up with a scoreboard the validator
cannot see, so the wiring lives here.

The context builder is the *shared* one: root rules plus the role's rules, plus (in
:class:`~app.agents.roles.base.RoleAgent`) the role's prompt asset. One builder per role
keeps the cached prefix stable per role, which is the whole point of the split.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from app.agents.base import AgentProvider, BindingResolver, Governor
from app.agents.context import ContextBuilder, DefaultContextBuilder, load_role_rules
from app.agents.registry import AgentRegistry
from app.agents.roles.architect import ArchitectAgent
from app.agents.roles.base import RoleAgent
from app.agents.roles.inputs import (
    NoReportSource,
    ReportSource,
    ReviewScoreboard,
    SectionTargetProvider,
    SectionTextProvider,
)
from app.agents.roles.researcher import ResearcherAgent
from app.agents.roles.reviewer import ReviewerAgent
from app.agents.roles.validator import ValidatorAgent
from app.agents.roles.writer import WriterAgent
from app.contracts.enums import AgentRole
from app.core.rubric import Rubric
from app.skills.loader import SkillLoader
from app.tools.base import ToolRegistry

__all__ = [
    "ROLE_AGENTS",
    "build_role_agent",
    "default_role_context_builder",
    "register_role_agents",
]

#: Role -> concrete class. The registry and the API both dispatch through this map.
ROLE_AGENTS: Final[dict[AgentRole, type[RoleAgent]]] = {
    AgentRole.ARCHITECT: ArchitectAgent,
    AgentRole.RESEARCHER: ResearcherAgent,
    AgentRole.WRITER: WriterAgent,
    AgentRole.REVIEWER: ReviewerAgent,
    AgentRole.VALIDATOR: ValidatorAgent,
}


def default_role_context_builder(
    role: AgentRole,
    *,
    skills: SkillLoader,
    rules_dir: Path | None = None,
    tools: ToolRegistry | None = None,
    project: str | None = None,
) -> ContextBuilder:
    """Root rules + the role's rules + the role's authorised tool schemas.

    The role's prompt asset is appended by :class:`RoleAgent` itself, so it is present
    even when a caller injects a context builder of its own.
    """
    root_rules, role_rules = load_role_rules(rules_dir or _rules_dir(), role)
    return DefaultContextBuilder(
        skills=skills, rules=(root_rules, role_rules), tools=tools, project=project
    )


def _rules_dir() -> Path:
    """``rules/`` at the repository root, resolved the same way prompts are."""
    from app.agents.roles.base import repository_root

    return repository_root() / "rules"


def build_role_agent(
    role: AgentRole,
    *,
    provider: AgentProvider,
    bindings: BindingResolver,
    governor: Governor,
    context_builder: ContextBuilder,
    rubric: Rubric | None = None,
    tools: ToolRegistry | None = None,
    project: str | None = None,
    section_texts: SectionTextProvider | None = None,
    scoreboard: ReviewScoreboard | None = None,
    reports: ReportSource | None = None,
    targets: SectionTargetProvider | None = None,
    prompt: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RoleAgent:
    """Build one role agent with exactly the collaborators that role needs."""
    common: dict[str, Any] = {
        "provider": provider,
        "bindings": bindings,
        "governor": governor,
        "context_builder": context_builder,
        "tools": tools,
        "project": project,
        "prompt": prompt,
    }
    if extra:
        common.update(extra)

    if role is AgentRole.ARCHITECT:
        return ArchitectAgent(rubric=rubric, **common)
    if role is AgentRole.RESEARCHER:
        return ResearcherAgent(**common)
    if role is AgentRole.WRITER:
        return WriterAgent(section_texts=section_texts, targets=targets, rubric=rubric, **common)
    if role is AgentRole.REVIEWER:
        return ReviewerAgent(
            section_texts=section_texts, scoreboard=scoreboard, rubric=rubric, **common
        )
    return ValidatorAgent(
        section_texts=section_texts,
        scoreboard=scoreboard,
        reports=reports or NoReportSource(),
        rubric=rubric,
        **common,
    )


def register_role_agents(
    registry: AgentRegistry,
    *,
    provider: AgentProvider,
    bindings: BindingResolver,
    governor: Governor,
    skills: SkillLoader,
    rules_dir: Path | None = None,
    rubric: Rubric | None = None,
    tools: ToolRegistry | None = None,
    project: str | None = None,
    section_texts: SectionTextProvider | None = None,
    scoreboard: ReviewScoreboard | None = None,
    reports: ReportSource | None = None,
    targets: SectionTargetProvider | None = None,
) -> ReviewScoreboard:
    """Register all five roles, sharing one scoreboard (reviewer writes, validator reads).

    Returns the scoreboard so the caller can inspect or clear it between runs.
    """
    shared = scoreboard or ReviewScoreboard()

    def factory_for(role: AgentRole) -> Callable[[], RoleAgent]:
        def build() -> RoleAgent:
            context_builder = default_role_context_builder(
                role,
                skills=skills,
                rules_dir=rules_dir,
                tools=tools,
                project=project,
            )
            return build_role_agent(
                role,
                provider=provider,
                bindings=bindings,
                governor=governor,
                context_builder=context_builder,
                rubric=rubric,
                tools=tools,
                project=project,
                section_texts=section_texts,
                scoreboard=shared,
                reports=reports,
                targets=targets,
            )

        return build

    for role in AgentRole:
        registry.register(role, factory_for(role))
    return shared
