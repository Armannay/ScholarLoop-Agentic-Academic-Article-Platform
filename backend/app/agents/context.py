"""Task context assembly: the stable prefix, the references, and nothing else.

``ContextBuilder`` is the seam the runtime calls before every provider call. The
default implementation is deliberately boring, because most of the token budget is
won or lost here:

* **The system blocks form a stable prefix.** Rules first, then the skill index, then
  the selected skill bodies, then the tool schemas allowed for the role. Identical
  inputs produce byte-identical blocks (``prefix_hash`` lets a test prove it), which is
  what makes prompt caching and diffing possible.
* **Only references travel, never article text.** The user message carries the task,
  the brief and a per-section state row (id, version, status, words, hash, issues).
  Section bodies live in the article store and are addressed by hash; they are not
  copied into the prompt (HARD RULE 3).
* **Skill selection is centralised.** The builder asks the loader; an agent cannot
  fetch a skill on its own. At most two bodies are rendered, and only when a trigger
  matched.
* **Tool schemas are authorised before they are rendered.** ``schemas_for`` applies
  the same role/project scope as execution, so the model never sees a tool it could
  not call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, MessageRole
from app.contracts.llm import LLMMessage, SystemBlock
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.hashing import content_hash
from app.providers.base import estimate_tokens
from app.skills.loader import SkillLoader
from app.tools.base import ToolRegistry

__all__ = [
    "TASK_INSTRUCTION",
    "AgentContext",
    "AgentRunState",
    "ContextBuilder",
    "DefaultContextBuilder",
    "RunState",
    "load_role_rules",
]

#: The one instruction that precedes the task payload. It is the only prose the
#: builder adds to the user message.
TASK_INSTRUCTION = (
    "Complete the task below. Return exactly one JSON object that conforms to the "
    "AgentResult schema: patches plus the ChangeEntry that describes them. Do not "
    "write anything outside the JSON object."
)


@runtime_checkable
class RunState(Protocol):
    """The minimum a context needs from the orchestrator's run state.

    A protocol, not a contract: the orchestrator owns the real run state, and the
    agent runtime only reads these four views. This is the seam that keeps Step 7 from
    having to reshape its state to fit the agent.
    """

    @property
    def iter(self) -> int:
        """Current loop iteration."""
        ...

    @property
    def brief(self) -> Brief | None:
        """The user's brief, when intake has produced one."""
        ...

    @property
    def sections(self) -> Sequence[SectionState]:
        """Every section's state, in article order."""
        ...

    @property
    def sources(self) -> Sequence[SourceCard]:
        """SourceCards gathered so far."""
        ...


@dataclass(frozen=True)
class AgentRunState:
    """A small concrete :class:`RunState`, for tests and for a first integration.

    The orchestrator may pass its own object instead; only the four properties above
    are read.
    """

    iter: int = 0
    brief: Brief | None = None
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.iter <= 10_000:
            raise ValueError(f"iter must be 0..10000, got {self.iter}")


@dataclass(frozen=True)
class AgentContext:
    """Assembled prompt material for one call, with a stable-prefix fingerprint."""

    system_blocks: tuple[SystemBlock, ...]
    messages: tuple[LLMMessage, ...]
    skills: tuple[str, ...]
    prefix_hash: str

    @property
    def prefix_tokens(self) -> int:
        """Estimated tokens in the cacheable system prefix."""
        return sum(estimate_tokens(block.text) for block in self.system_blocks)


@runtime_checkable
class ContextBuilder(Protocol):
    """Builds the request material for one task execution."""

    def build(self, *, task: Task, run_state: RunState) -> AgentContext:
        """Return system blocks and messages for ``task`` in ``run_state``."""
        ...


def load_role_rules(rules_dir: Path | str, role: AgentRole) -> tuple[str, str]:
    """Read the shared root rules and the role's own rules.

    Raises:
        ConfigError: either file is missing. A missing rules file is a deployment
            error, not a reason to run an agent without its hard rules.
    """
    directory = Path(rules_dir)
    root_path = directory / "root.rules.md"
    role_path = directory / "agents" / f"{role.value}.rules.md"
    for path in (root_path, role_path):
        if not path.is_file():
            raise ConfigError(f"rules file not found: {path}")
    return (
        root_path.read_text(encoding="utf-8").strip(),
        role_path.read_text(encoding="utf-8").strip(),
    )


def _section_row(state: SectionState) -> dict[str, Any]:
    """A reference row, not the section body."""
    return {
        "id": state.id,
        "title": state.title,
        "version": state.version,
        "status": state.status.value,
        "words": state.words,
        "hash": state.hash,
        "open_issues": list(state.open_issues),
    }


def _source_row(card: SourceCard) -> dict[str, Any]:
    """Metadata a writer needs to cite a card; the snippet stays in the source store."""
    return {
        "id": card.id,
        "title": card.title,
        "year": card.year,
        "venue": card.venue,
        "doi": card.doi,
        "verification": card.verification.value,
    }


class DefaultContextBuilder:
    """Assemble rules, skill index, selected skills, tool schemas and task references.

    Args:
        skills: the central skill loader (the only way skills enter a prompt).
        rules: rule texts in prompt order, usually root rules then role rules.
        tools: optional scoped registry; when present, only the role's allowed
            schemas are rendered.
        project: project name used for tool scope resolution.
        max_selected_skills: override for the loader's selection cap (still <= 2).
        skill_token_budget: override for the loader's per-task skill budget.
    """

    def __init__(
        self,
        *,
        skills: SkillLoader,
        rules: Sequence[str] = (),
        tools: ToolRegistry | None = None,
        project: str | None = None,
        max_selected_skills: int | None = None,
        skill_token_budget: int | None = None,
    ) -> None:
        self._skills = skills
        self._rules = tuple(rule.strip() for rule in rules if rule.strip())
        self._tools = tools
        self._project = project
        self._max_selected_skills = max_selected_skills
        self._skill_token_budget = skill_token_budget

    def build(self, *, task: Task, run_state: RunState) -> AgentContext:
        """Return a deterministic context for ``task``."""
        blocks: list[SystemBlock] = [SystemBlock(text=rule, cache=True) for rule in self._rules]
        blocks.append(
            SystemBlock(text=f"## Skill index\n{self._skills.render_index()}", cache=True)
        )

        selection = self._skills.select(
            task,
            max_skills=self._max_selected_skills,
            token_budget=self._skill_token_budget,
        )
        if selection.skills:
            blocks.append(SystemBlock(text=selection.render(), cache=True))

        if self._tools is not None:
            schemas = self._tools.schemas_for(task.role, project=self._project)
            if schemas:
                rendered = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
                text = f"## Tool schemas (authorised for this role)\n{rendered}"
                blocks.append(SystemBlock(text=text, cache=True))

        message = LLMMessage(role=MessageRole.USER, text=self._user_text(task, run_state))
        prefix_hash = content_hash("\n\n".join(block.text for block in blocks))
        return AgentContext(
            system_blocks=tuple(blocks),
            messages=(message,),
            skills=selection.names,
            prefix_hash=prefix_hash,
        )

    def _user_text(self, task: Task, run_state: RunState) -> str:
        """The per-task message: references and acceptance criteria, no prose bodies."""
        payload: dict[str, Any] = {
            "task": task.model_dump(mode="json"),
            "brief": run_state.brief.model_dump(mode="json") if run_state.brief else None,
            "sections": [_section_row(state) for state in run_state.sections],
            "sources": [_source_row(card) for card in run_state.sources],
        }
        return f"{TASK_INSTRUCTION}\n\n" + json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
