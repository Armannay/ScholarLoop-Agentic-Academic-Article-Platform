"""Role implementations on the shared Step 5 runtime.

The envelope schema is enforced by Agent; these output schemas are also available to
orchestrator/UI consumers for finer role-specific validation. The call-time binding
controls the concrete model; tiers below are defaults, not hard-coded model IDs.
"""
from __future__ import annotations

from typing import ClassVar

from app.agents.base import Agent
from app.contracts.enums import AgentRole, BindingRole, ModelTier


class RoleAgent(Agent):
    """Shared metadata for concrete roles; execution stays in the generic runtime."""
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    default_tier: ClassVar[ModelTier]
    max_input_tokens: ClassVar[int] = 16_000
    max_output_tokens: ClassVar[int] = 3_000
    output_model: ClassVar[type]


class ArchitectAgent(RoleAgent):
    role = AgentRole.ARCHITECT
    default_tier = ModelTier.MID
    max_output_tokens = 4_000
    output_model: ClassVar[type]
    allowed_tools = ()


class ResearcherAgent(RoleAgent):
    role = AgentRole.RESEARCHER
    default_tier = ModelTier.MID
    allowed_tools = ("scholar_search",)
    output_model: ClassVar[type]


class WriterAgent(RoleAgent):
    role = AgentRole.WRITER
    default_tier = ModelTier.MID
    max_output_tokens = 8_000
    output_model: ClassVar[type]
    allowed_tools = ()


class ReviewerAgent(RoleAgent):
    role = AgentRole.REVIEWER
    default_tier = ModelTier.STRONG
    max_output_tokens = 4_000
    output_model: ClassVar[type]
    allowed_tools = ()


class ValidatorAgent(RoleAgent):
    role = AgentRole.VALIDATOR
    default_tier = ModelTier.CHEAP
    max_output_tokens = 2_000
    allowed_tools = ("text_checks", "citation_check", "doi_verify")
    output_model: ClassVar[type]


# Binding-role values are deliberately explicit and auditable.
ROLE_BINDINGS: dict[AgentRole, BindingRole] = {role: BindingRole(role.value) for role in AgentRole}

from app.agents.roles.schemas import (  # noqa: E402
    ArchitectOutput, ResearcherOutput, ReviewerOutput, ValidatorOutput, WriterOutput,
)

ArchitectAgent.output_model = ArchitectOutput
ResearcherAgent.output_model = ResearcherOutput
WriterAgent.output_model = WriterOutput
ReviewerAgent.output_model = ReviewerOutput
ValidatorAgent.output_model = ValidatorOutput
