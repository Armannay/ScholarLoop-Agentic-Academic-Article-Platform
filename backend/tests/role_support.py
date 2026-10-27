"""Scaffolding shared by the role-agent tests: fake tools, a recorder, a real wiring.

Three rules keep the tests honest:

* **No live provider, no live tool.** The scholar search double answers with the cards it
  was constructed from; the validator gets the *real* ``text_checks`` and
  ``citation_check`` tools, which are pure functions over text and the rubric.
* **One wiring, one shape.** :func:`build_agent` builds a role through
  :func:`app.agents.roles.factory.build_role_agent`, exactly as production does, so a test
  cannot pass by assembling an agent production would never build.
* **Fixtures are the input.** Every role is driven from ``prompts/<role>.golden.json``
  through :class:`~app.agents.roles.golden.GoldenCase`, so the example a reader sees and
  the answer a test replays are the same bytes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from app.agents.binding import MappingBindingResolver
from app.agents.governor import CallBudgetGovernor
from app.agents.roles.base import RoleAgent
from app.agents.roles.factory import build_role_agent, default_role_context_builder
from app.agents.roles.golden import GoldenCase
from app.agents.roles.inputs import (
    NoReportSource,
    ReportSource,
    ReviewScoreboard,
    SectionTargetProvider,
    SectionTextProvider,
)
from app.contracts.binding import AgentBinding
from app.contracts.enums import AgentRole, BindingRole, BindingStrategy
from app.contracts.enums import AgentRole as Role
from app.contracts.llm import LLMRequest, LLMResponse
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.providers.mock import MockProvider
from app.skills.loader import SkillLoader
from app.tools.base import ToolContext, ToolRegistry, ToolResult
from app.tools.citation_check import CitationCheckTool
from app.tools.text_checks import TextChecksTool

__all__ = [
    "ACCOUNT_ID",
    "FakeScholarSearch",
    "RecordingProvider",
    "agent_binding",
    "build_agent",
    "context_builder_for",
    "governor",
    "mutated_answer",
    "researcher_tools",
    "scripted_provider",
    "validator_tools",
]

#: A deterministic ULID for the account every test binding points at.
ACCOUNT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def context_builder_for(role: AgentRole, repo_root: Path, *, tools: ToolRegistry | None = None):
    """The production context builder for ``role``, pointed at the repository's assets."""
    return default_role_context_builder(
        role,
        skills=SkillLoader(repo_root / "skills"),
        rules_dir=repo_root / "rules",
        tools=tools,
    )


def governor(policies: Policies) -> CallBudgetGovernor:
    """The Step 5 governor seam, enforcing the shipped policies."""
    return CallBudgetGovernor(policies)


def agent_binding(
    role: AgentRole,
    *,
    model: str = "mock-role-1",
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 8_000,
) -> AgentBinding:
    """A dedicated binding for one role (role caps still clamp the request)."""
    return AgentBinding(
        role=BindingRole(role.value),
        account_ids=[ACCOUNT_ID],
        strategy=BindingStrategy.DEDICATED,
        model=model,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )


def scripted_provider(role: AgentRole, *answers: str) -> MockProvider:
    """A ``MockProvider`` answering ``role``'s calls with ``answers``, in order."""
    provider = MockProvider()
    for index, answer in enumerate(answers):
        provider.script_reply(role.value, index, answer)
    return provider


def build_agent(
    role: AgentRole,
    *,
    repo_root: Path,
    policies: Policies,
    provider: Any,
    rubric: Rubric | None = None,
    binding: AgentBinding | None = None,
    tools: ToolRegistry | None = None,
    section_texts: SectionTextProvider | None = None,
    scoreboard: ReviewScoreboard | None = None,
    reports: ReportSource | None = None,
    targets: SectionTargetProvider | None = None,
    prompt: str | None = None,
) -> RoleAgent:
    """Build ``role`` the way production does, with test-controlled collaborators."""
    effective = binding or agent_binding(role)
    return build_role_agent(
        role,
        provider=provider,
        bindings=MappingBindingResolver({BindingRole(role.value): effective}),
        governor=governor(policies),
        context_builder=context_builder_for(role, repo_root, tools=tools),
        rubric=rubric,
        tools=tools,
        section_texts=section_texts,
        scoreboard=scoreboard,
        reports=reports or NoReportSource(),
        targets=targets,
        prompt=prompt,
    )


def mutated_answer(
    case: GoldenCase,
    *,
    output: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> str:
    """The fixture's answer with selected top-level keys replaced."""
    payload: dict[str, Any] = {
        "result": json.loads(json.dumps(case.expected.result)),
        "output": json.loads(json.dumps(case.expected.output)),
    }
    if result:
        payload["result"].update(result)
    if output:
        payload["output"].update(output)
    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------------------- tools
class FakeScholarSearch:
    """A deterministic ``scholar_search``: returns the cards it was given, in order.

    It records the queries, because "the role searched before it wrote" is a property
    worth asserting without reaching for a live index.
    """

    name = "scholar_search"
    description = "Return the fixture's cards for any query."
    token_cost = 10
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 500},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, cards: Sequence[dict[str, Any]] = ()) -> None:
        self.cards = [dict(card) for card in cards]
        self.queries: list[str] = []

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        """Record the query and answer with at most ``max_results`` cards."""
        _ = context
        self.queries.append(str(arguments.get("query", "")))
        limit = int(arguments.get("max_results", len(self.cards) or 1))
        cards = self.cards[:limit]
        payload = {
            "cache_hit": False,
            "count": len(cards),
            "result_limited": len(cards) < len(self.cards),
            "sources": cards,
        }
        return ToolResult(
            tool=self.name, content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )


def researcher_tools(cards: Sequence[dict[str, Any]] = ()) -> ToolRegistry:
    """A registry granting the researcher (and only the researcher) the fake search."""
    return ToolRegistry(
        [FakeScholarSearch(cards)], role_tools={Role.RESEARCHER: {"scholar_search"}}
    )


def validator_tools(rubric: Rubric) -> ToolRegistry:
    """The real deterministic validator tools, granted to the validator only."""
    return ToolRegistry(
        [TextChecksTool(rubric=rubric), CitationCheckTool()],
        role_tools={Role.VALIDATOR: {"text_checks", "citation_check"}},
    )


# -------------------------------------------------------------------------- provider
class RecordingProvider:
    """Delegates to a ``MockProvider`` and keeps every request it was sent."""

    def __init__(self, inner: MockProvider) -> None:
        self._inner = inner
        self.requests: list[LLMRequest] = []
        self.models: list[str | None] = []
        self.roles: list[str | None] = []

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
    ) -> LLMResponse:
        """Record the request, then answer with the scripted reply."""
        self.requests.append(request)
        self.models.append(model)
        self.roles.append(role)
        return await self._inner.generate(request, model=model, role=role)

    @property
    def call_count(self) -> int:
        """How many calls were attempted."""
        return len(self.requests)

    @property
    def first_system_text(self) -> str:
        """Every system block of the first request, joined (what the model first saw)."""
        if not self.requests:
            return ""
        return "\n\n".join(block.text for block in self.requests[0].system_blocks)

    def system_text(self) -> str:
        """All system-block text across every request (what the model saw)."""
        return "\n\n".join(
            block.text for request in self.requests for block in request.system_blocks
        )
