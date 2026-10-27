"""Architect: turn a Brief into an ordered plan of sections, once per Planner request.

The architect is the only role that decides *structure*. It reads the Brief and the
rubric's section list for the Brief's article type and returns:

* ``result.sections`` - one empty :class:`~app.contracts.section.SectionState` seed per
  section, in reading order (id, title, order, status ``empty``);
* ``output`` - the plan itself: per-section goal, word target, claims map and required
  sources (:class:`~app.agents.roles.schemas.SectionSeed`).

The two halves exist because CONTRACTS v1 has nowhere to put a goal or a claim map
(CCR-7.1). What the orchestrator stores becomes ``SectionState``; what later roles
*want* is the plan, which is why the goal and claims travel next to it.

Re-running is a Planner decision, and the architect guards it: a second pass may add
sections, but it may not drop or reorder a section that already carries text. Structure
is cheap to change early and expensive to change after three writers have drafted
against it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar

from app.agents.context import RunState
from app.agents.roles.base import RoleAgent
from app.agents.roles.schemas import ArchitectOutput
from app.contracts.agent import AgentResult
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, ModelTier, SectionStatus
from app.contracts.llm import SystemBlock
from app.contracts.task import Task
from app.core.rubric import Rubric, SectionSpec, resolve_sections

__all__ = ["WORD_TOLERANCE", "ArchitectAgent"]

#: The total-words gate allows ±15 % of ``Brief.target_words`` (config/rubric.yaml).
WORD_TOLERANCE: float = 0.15


class ArchitectAgent(RoleAgent):
    """Plan the article's sections from the Brief. Never writes prose, never searches."""

    role = AgentRole.ARCHITECT
    default_tier = ModelTier.MID
    output_model: ClassVar[type[Contract]] = ArchitectOutput
    #: The architect returns structure, never text: the schema does not offer these.
    forbidden_result_fields: ClassVar[tuple[str, ...]] = ("patches", "source_cards", "report")
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    max_input_tokens: ClassVar[int] = 16_000
    max_output_tokens: ClassVar[int] = 4_000

    def __init__(self, *, rubric: Rubric | None = None, **kwargs: Any) -> None:
        """``rubric`` turns the section list into a checked contract; without it the
        architect still plans, but cannot refuse an id the rubric does not know."""
        self._rubric = rubric
        super().__init__(**kwargs)

    # -- preparation ------------------------------------------------------
    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Give the model the rubric's section list for this article type."""
        self.require_brief(run_state)
        run = self._scratch(task)
        specs = self._specs(run_state)
        if specs:
            payload = {
                "article_type": run_state.brief.article_type.value if run_state.brief else None,
                "target_words": run_state.brief.target_words if run_state.brief else None,
                "sections": [
                    {
                        "id": spec.id,
                        "title": spec.title,
                        "required": spec.required,
                        "min_words": spec.min_words,
                        "max_words": spec.max_words,
                        "target_words": spec.target_words,
                    }
                    for spec in specs
                ],
            }
            run.block(
                "Rubric sections for this article type",
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        return run.blocks

    def _specs(self, run_state: RunState) -> list[SectionSpec]:
        """Resolved section specs: the rubric's list plus the Brief's must-include items."""
        brief = run_state.brief
        if self._rubric is None or brief is None:
            return []
        return resolve_sections(
            self._rubric, brief.article_type, extra_titles=list(brief.must_include)
        )

    # -- acceptance -------------------------------------------------------
    def output_problems(
        self,
        output: Contract,
        *,
        result: AgentResult,
        task: Task,
        run_state: RunState,
    ) -> list[str]:
        """Check the outline against the Brief, the rubric and the existing article."""
        assert isinstance(output, ArchitectOutput)
        problems: list[str] = []

        seed_ids = [state.id for state in result.sections]
        if seed_ids != output.ordered_ids:
            problems.append(
                f"result.sections must mirror the outline in order: expected "
                f"{output.ordered_ids}, got {seed_ids}"
            )
        for state in result.sections:
            if state.status is not SectionStatus.EMPTY:
                problems.append(
                    f"result.sections[{state.id}].status: seeds are empty sections, got "
                    f"'{state.status.value}'"
                )
        for state, seed in zip(result.sections, output.sections, strict=False):
            if state.title != seed.title:
                problems.append(
                    f"result.sections[{state.id}].title: '{state.title}' does not match the "
                    f"outline's '{seed.title}'"
                )

        brief = run_state.brief
        if brief is not None:
            total = output.total_target_words
            low = int(brief.target_words * (1 - WORD_TOLERANCE))
            high = int(brief.target_words * (1 + WORD_TOLERANCE))
            if not low <= total <= high:
                problems.append(
                    f"output.sections.target_words: the outline plans {total} words; the "
                    f"brief asks for {brief.target_words} and the gate allows {low}..{high}"
                )

        specs = self._specs(run_state)
        if specs:
            allowed = {spec.id for spec in specs}
            unknown = [section_id for section_id in output.ordered_ids if section_id not in allowed]
            if unknown:
                problems.append(
                    f"output.sections.id: {unknown} are not rubric sections for this article "
                    f"type; use the rubric's ids verbatim"
                )
            planned = set(output.ordered_ids)
            missing = [spec.id for spec in specs if spec.required and spec.id not in planned]
            if missing:
                problems.append(f"output.sections: required rubric sections are missing: {missing}")

        problems.extend(self._rerun_problems(output, run_state))
        return problems

    def _rerun_problems(self, output: ArchitectOutput, run_state: RunState) -> list[str]:
        """A second pass may add sections; it may not drop or reorder drafted ones."""
        drafted = [state for state in run_state.sections if state.version > 0]
        if not drafted:
            return []
        planned = output.ordered_ids
        dropped = [state.id for state in drafted if state.id not in planned]
        if dropped:
            return [
                f"output.sections: this is a re-plan and {dropped} already contain text; a "
                "new structure may add sections but never drop drafted work"
            ]
        positions = [planned.index(state.id) for state in drafted]
        if positions != sorted(positions):
            return [
                "output.sections: a re-plan may not reorder sections that already contain "
                "text; reordering rewrites the article's argument by accident"
            ]
        return []
