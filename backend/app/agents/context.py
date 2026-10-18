"""Task context: the bounded prompt an agent receives.

HARD RULE 3 in one module. An agent never receives the article. It receives:

* the **brief** (topic, audience, language, citation style, what must and must not
  appear, the user's own data);
* the **task** (title, instructions, acceptance criteria, the section ids and
  source ids it may work on);
* the **state** of those sections (id, version, word count, status, open issues) -
  never their text;
* the **cards** of the sources it may cite (id, authors, year, venue, locator and a
  snippet of at most 60 words);
* the **digest** of what happened so far (bounded, written by the memory layer).

Everything is a reference. The agent reads the article through the article store
and writes to it through patches, so a prompt is a list of pointers rather than a
copy of the manuscript.

The builder is deliberately dumb about *content*: it composes strings from
contracts, caps them, and hands the result to the governor, which decides whether
the prompt still fits into the call budget. What it must never do is invent:
everything here comes from a contract, and a missing piece is an error, not a
guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import Field

from app.contracts.brief import Brief
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, MessageRole
from app.contracts.llm import LLMMessage, LLMRequest, SystemBlock
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError, ScholarLoopError

__all__ = ["ContextError", "TaskContext", "TaskContextBuilder"]

#: How many source cards are listed at most. The researcher hands out cards by id;
#: a writer that needs more should ask for a specific card in a follow-up task.
MAX_SOURCES: Final = 24
#: How many section states are listed at most.
MAX_SECTIONS: Final = 32
#: How many characters of the digest are pasted into the prompt.
MAX_DIGEST_CHARS: Final = 4_000
#: How many characters of the user's own data are pasted into the prompt.
MAX_PROVIDED_DATA_CHARS: Final = 2_000
#: Default output cap for an agent call.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4_000


class ContextError(ScholarLoopError):
    """The run state does not carry what an agent needs to work."""

    code = "context_error"
    http_status = 500


class TaskContext(Contract):
    """Everything an agent may know about the run, as references.

    This is the seam between the orchestrator (which owns the article, the sources
    and the digest) and the agent runtime (which owns the prompt). The
    orchestrator builds one of these per task; the builder turns it into a request.
    """

    brief: Brief
    sections: list[SectionState] = Field(default_factory=list, max_length=64)
    sources: list[SourceCard] = Field(default_factory=list, max_length=200)
    digest: str | None = Field(default=None, max_length=20_000)
    iteration: int = Field(default=0, ge=0, le=10_000)


class TaskContextBuilder:
    """Assemble the request for one task from references, never from article text.

    The rules files (``rules/root.rules.md`` and ``rules/agents/<role>.rules.md``)
    are the stable prefix of the prompt: they are read once, cached, and marked
    ``cache=True`` so a cache-aware provider can keep them out of the billable
    prompt.
    """

    def __init__(
        self,
        *,
        rules_dir: Path,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_sources: int = MAX_SOURCES,
        max_sections: int = MAX_SECTIONS,
        json_schema: dict[str, Any] | None = None,
    ) -> None:
        self._rules_dir = rules_dir
        self._max_output_tokens = max_output_tokens
        self._max_sources = max_sources
        self._max_sections = max_sections
        self._json_schema = json_schema
        self._rules_cache: dict[Path, tuple[tuple[int, int], str]] = {}

    # -- public API --------------------------------------------------------
    def build(self, task: Task, run_state: object) -> LLMRequest:
        """Build the request for ``task``.

        Args:
            task: the task to execute; it fixes the role and the acceptance
                criteria the agent is graded against.
            run_state: the run state. Accepted as a :class:`TaskContext`, as a
                mapping with the same keys, or as an object with the same
                attributes - the orchestrator owns that shape.

        Returns:
            An :class:`LLMRequest` whose prompt contains references only.

        Raises:
            ContextError: the run state carries no brief, so the agent would have
                to guess what the article is about.
            ConfigError: a rules file is missing.
        """
        context = self._context_from(run_state)

        system_blocks = [
            SystemBlock(text=self._root_rules(), cache=True),
            SystemBlock(text=self._role_rules(task.role), cache=True),
            SystemBlock(
                text=(
                    "Answer with a single JSON object that conforms to the supplied "
                    "schema. No prose, no markdown fences, no commentary."
                ),
                cache=True,
            ),
        ]
        return LLMRequest(
            system_blocks=system_blocks,
            messages=[LLMMessage(role=MessageRole.USER, text=self._compose(context, task))],
            max_output_tokens=self._max_output_tokens,
            json_schema=self._json_schema,
        )

    # -- run state ---------------------------------------------------------
    def _context_from(self, run_state: object) -> TaskContext:
        """Read a :class:`TaskContext` out of whatever shape the caller used."""
        if isinstance(run_state, TaskContext):
            return run_state

        if isinstance(run_state, Mapping):
            brief = run_state.get("brief")
            sections = run_state.get("sections") or []
            sources = run_state.get("sources") or []
            digest = run_state.get("digest") or ""
            iteration = run_state.get("iteration") or run_state.get("iter") or 0
        else:
            brief = getattr(run_state, "brief", None)
            sections = getattr(run_state, "sections", None) or []
            sources = getattr(run_state, "sources", None) or []
            digest = getattr(run_state, "digest", "") or ""
            iteration = getattr(run_state, "iteration", None) or getattr(run_state, "iter", 0)

        if brief is None:
            raise ContextError("the run state carries no brief; an agent cannot work without one")
        if not isinstance(brief, Brief):
            brief = Brief.model_validate(brief)

        return TaskContext(
            brief=brief,
            sections=[self._as_section(item) for item in sections],
            sources=[self._as_source(item) for item in sources],
            digest=(str(digest)[: MAX_DIGEST_CHARS * 5] or None) if digest else None,
            iteration=int(iteration) if isinstance(iteration, int) else 0,
        )

    @staticmethod
    def _as_section(item: Any) -> SectionState:
        """Coerce a section state, accepting both the model and a mapping."""
        if isinstance(item, SectionState):
            return item
        return SectionState.model_validate(item)

    @staticmethod
    def _as_source(item: Any) -> SourceCard:
        """Coerce a source card, accepting both the model and a mapping."""
        if isinstance(item, SourceCard):
            return item
        return SourceCard.model_validate(item)

    # -- rules -------------------------------------------------------------
    def _root_rules(self) -> str:
        """The rules every agent obeys."""
        return self._read_rules(self._rules_dir / "root.rules.md")

    def _role_rules(self, role: AgentRole) -> str:
        """The rules of one role."""
        return self._read_rules(self._rules_dir / "agents" / f"{role.value}.rules.md")

    def _read_rules(self, path: Path) -> str:
        """Read a rules file, caching it until the file changes on disk."""
        try:
            stat = path.stat()
        except OSError as exc:
            raise ConfigError(f"rules file not found: {path}") from exc

        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._rules_cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        text = path.read_text(encoding="utf-8")
        self._rules_cache[path] = (stamp, text)
        return text

    # -- composition -------------------------------------------------------
    def _compose(self, context: TaskContext, task: Task) -> str:
        """Build the user message: brief, task, references, digest."""
        lines: list[str] = []
        lines.extend(self._brief_lines(context.brief))
        lines.extend(self._task_lines(task))
        lines.extend(self._section_lines(context.sections))
        lines.extend(self._source_lines(context.sources))
        lines.extend(self._digest_lines(context.digest))
        return "\n\n".join(block for block in lines if block)

    def _brief_lines(self, brief: Brief) -> list[str]:
        """The brief, in the few lines an agent actually needs."""
        block = [
            "## ARTICLE",
            f"topic: {brief.topic}",
            f"type: {brief.article_type.value}",
            f"audience: {brief.audience}",
            f"language: {brief.language}",
            f"citation style: {brief.citation_style.value}",
            f"target words: {brief.target_words} (allowed {brief.word_tolerance_low}"
            f"-{brief.word_tolerance_high})",
        ]
        if brief.must_include:
            block.append("must include: " + "; ".join(brief.must_include))
        if brief.must_avoid:
            block.append("must avoid: " + "; ".join(brief.must_avoid))
        if brief.provided_data:
            data = "\n".join(f"- {item}" for item in brief.provided_data)
            block.append(
                "provided data (user-supplied, citable as-is):\n" + data[:MAX_PROVIDED_DATA_CHARS]
            )
        return ["\n".join(block)]

    @staticmethod
    def _task_lines(task: Task) -> list[str]:
        """The task: what to do and how it is graded."""
        block = [
            "## TASK",
            f"id: {task.id}",
            f"role: {task.role.value}",
            f"title: {task.title}",
            f"instructions: {task.instructions}",
            "acceptance:",
            *[f"- {item}" for item in task.acceptance],
        ]
        if task.section_ids:
            block.append("sections: " + ", ".join(task.section_ids))
        if task.source_ids:
            block.append("source ids: " + ", ".join(task.source_ids))
        block.append(f"token budget: {task.token_budget}")
        return ["\n".join(block)]

    def _section_lines(self, sections: Sequence[SectionState]) -> list[str]:
        """Section states - ids and metrics, never text."""
        if not sections:
            return []
        block = ["## SECTION STATE (references only; read the text from the article store)"]
        for section in sections[: self._max_sections]:
            line = (
                f"- {section.id} | v{section.version} | {section.words} words | "
                f"{section.status.value}"
            )
            if section.open_issues:
                line += f" | open issues: {', '.join(section.open_issues)}"
            block.append(line)
        return ["\n".join(block)]

    def _source_lines(self, sources: Sequence[SourceCard]) -> list[str]:
        """Source cards the agent may cite."""
        if not sources:
            return []
        block = ["## SOURCES (cite by id; never invent a source that is not here)"]
        for card in sources[: self._max_sources]:
            authors = ", ".join(card.authors) if card.authors else "unattributed"
            locator = card.doi or card.url or ""
            block.append(
                f"- {card.id} | {authors} ({card.year}) | {card.venue} | "
                f"{locator} | {card.verification.value}"
            )
            if card.snippet:
                block.append(f"  snippet: {card.snippet}")
        return ["\n".join(block)]

    def _digest_lines(self, digest: str | None) -> list[str]:
        """The bounded history the memory layer produced."""
        if not digest or not digest.strip():
            return []
        return ["## HISTORY (bounded digest of previous iterations)\n" + digest[:MAX_DIGEST_CHARS]]
