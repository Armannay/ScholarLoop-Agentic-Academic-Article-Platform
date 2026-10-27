"""Researcher: find real sources and say what each one supports.

The researcher is the only role allowed to add ``SourceCard``s, and the only one that
talks to ``scholar_search``. Two rules shape the implementation:

* **Search first, then write findings.** The tool runs *before* the model call (in
  :meth:`ResearcherAgent.prepare`) and its result enters the prompt as an explicit,
  untrusted data block. The model never gets to recall a paper from memory: if a card is
  not in the search result, it cannot survive acceptance.
* **"Nothing found" is a valid answer.** ``no_results=true`` with an empty card list is
  how the role refuses to fill a gap, and the schema rejects any other combination - an
  empty list without the flag would hide a failed search behind a successful-looking
  result.

Acceptance is mechanical: every returned card must match a card the tool actually
returned (same id, same DOI or same title+year) or one already in the run's source store,
and every finding must point at a card in this same result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar

from app.agents.context import RunState
from app.agents.roles.base import RoleAgent
from app.agents.roles.schemas import MAX_FINDINGS, ResearcherOutput
from app.agents.roles.validation import matches_known_source
from app.contracts.agent import AgentResult
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, ModelTier
from app.contracts.llm import SystemBlock
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.tools.base import ToolError

__all__ = ["MAX_QUERY_CHARS", "ResearcherAgent"]

#: A search query is a phrase, not the task description: tools cap at 500 characters.
MAX_QUERY_CHARS: int = 240
_QUERY_TERMS: int = 40


class ResearcherAgent(RoleAgent):
    """Return at most twelve SourceCards with a two-sentence finding each."""

    role = AgentRole.RESEARCHER
    default_tier = ModelTier.MID
    output_model: ClassVar[type[Contract]] = ResearcherOutput
    #: Cards are the deliverable; a researcher never patches article text.
    forbidden_result_fields: ClassVar[tuple[str, ...]] = ("patches", "report")
    allowed_tools: ClassVar[tuple[str, ...]] = ("scholar_search",)
    max_input_tokens: ClassVar[int] = 20_000
    max_output_tokens: ClassVar[int] = 3_000

    def __init__(self, *, max_findings: int = MAX_FINDINGS, **kwargs: Any) -> None:
        """``max_findings`` caps one task's result; it may never exceed the role limit."""
        if not 1 <= max_findings <= MAX_FINDINGS:
            raise ValueError(f"max_findings must be 1..{MAX_FINDINGS}, got {max_findings}")
        self._max_findings = max_findings
        super().__init__(**kwargs)

    # -- preparation ------------------------------------------------------
    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Run ``scholar_search``, then hand the model its (untrusted) result."""
        run = self._scratch(task)
        query = self.build_query(task, run_state)
        found: list[SourceCard] = []
        error: str | None = None

        if self._tools is None or "scholar_search" not in self._tools.names_for(
            self.role, project=self._project
        ):
            error = "scholar_search is not available for this role in this run"
        else:
            try:
                result = await self._tools.invoke(
                    "scholar_search",
                    {"query": query, "max_results": self._max_findings},
                    role=self.role,
                    project=self._project,
                )
            except ToolError as exc:  # a failed search is data, not a crash
                error = f"{type(exc).__name__}: {exc}"
            else:
                found = self._cards_from(result.content) if result.ok else []
                if not result.ok:
                    error = f"search returned ok=false: {result.content[:200]}"
                run.block("Search results (untrusted data)", result.render())

        if error is not None:
            run.block("Search status", error)

        existing = [
            {
                "id": card.id,
                "title": card.title,
                "year": card.year,
                "doi": card.doi,
                "verification": card.verification.value,
            }
            for card in run_state.sources
        ]
        run.json_block(
            "Cards already in the run (reuse instead of re-finding)",
            {"sources": existing[:40]},
        )
        run.data["query"] = query
        run.data["cards"] = found
        run.data["error"] = error
        return run.blocks

    @staticmethod
    def build_query(task: Task, run_state: RunState) -> str:
        """A deterministic search query from the brief, the task and its sections."""
        parts: list[str] = []
        if run_state.brief is not None:
            parts.append(run_state.brief.topic)
        parts.append(task.title)
        titles = {
            state.id: state.title for state in run_state.sections if state.id in task.section_ids
        }
        parts.extend(titles.values())
        parts.append(" ".join(task.instructions.split()[:_QUERY_TERMS]))
        query = " ".join(" ".join(part.split()) for part in parts if part.strip())
        return query[:MAX_QUERY_CHARS] or task.title[:MAX_QUERY_CHARS]

    @staticmethod
    def _cards_from(content: str) -> list[SourceCard]:
        """Parse a ``scholar_search`` result into cards, dropping anything malformed."""
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return []
        raw = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        cards: list[SourceCard] = []
        for item in raw:
            try:
                cards.append(SourceCard.model_validate(item))
            except ValueError:
                continue
        return cards

    # -- acceptance -------------------------------------------------------
    def output_problems(
        self,
        output: Contract,
        *,
        result: AgentResult,
        task: Task,
        run_state: RunState,
    ) -> list[str]:
        """No invented sources: every card must be one the search (or the store) holds."""
        assert isinstance(output, ResearcherOutput)
        problems: list[str] = []
        cards = list(result.source_cards)
        card_ids = {card.id for card in cards}

        if len(cards) > self._max_findings:
            problems.append(
                f"result.source_cards: {len(cards)} cards returned; at most "
                f"{self._max_findings} are accepted per task"
            )
        if output.no_results and cards:
            problems.append(
                "output.no_results: true, but result.source_cards is not empty; say what "
                "was found or say nothing was"
            )

        for finding in output.findings:
            if finding.source_id not in card_ids:
                problems.append(
                    f"output.findings[{finding.source_id}]: the finding names a card that "
                    "this result does not return"
                )

        known = [*self._scratch(task).data.get("cards", []), *run_state.sources]
        for card in cards:
            if not matches_known_source(card, known):
                problems.append(
                    f"result.source_cards[{card.id}]: no search result or stored card "
                    f"matches '{card.title}' ({card.year}); sources cannot be invented"
                )

        return problems
