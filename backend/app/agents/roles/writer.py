"""Writer: one section per task, a patch not a rewrite, citations that resolve.

Five properties are enforced mechanically, because they are the ones a plausible-sounding
model answer gets wrong:

* **One section.** A task with two ``section_ids`` is a planning error and is refused
  before the call, not silently narrowed to the first.
* **A patch, and the right *kind* of patch.** An empty section is filled with
  ``replace_section``; a section already reviewed is only ever edited with targeted ops.
  Replacing reviewed prose hides the delta the reviewer is going to look for.
* **Only supplied cards are citable.** Every ``[[CITE:id]]`` marker must name a card the
  task supplied or the run already holds - the same ids the tool-based citation gate will
  check later. The role output must name exactly the ids the text cites.
* **The word target, ±10 %.** Measured on the text the patch would leave behind, so a
  targeted edit is judged on the result rather than on how much was typed.
* **"I need input" is an answer, not a failure.** ``needs_user_input`` must arrive with
  no patch and a named missing input, so the loop can ask the user something specific.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from app.agents.context import RunState
from app.agents.roles.base import RoleAgent
from app.agents.roles.errors import RoleTaskInvalid
from app.agents.roles.inputs import (
    RubricSectionTargets,
    SectionTargetProvider,
    SectionTextProvider,
)
from app.agents.roles.schemas import WriterOutput
from app.agents.roles.validation import (
    allowed_source_ids,
    cite_marker_ids,
    estimated_words,
    inserted_text,
    placeholder_hits,
    section_by_id,
    supplied_source_cards,
)
from app.contracts.agent import AgentResult
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, ChangeStatus, ModelTier, PatchOpType, SectionStatus
from app.contracts.llm import SystemBlock
from app.contracts.patch import Patch, PatchOp
from app.contracts.section import SectionState
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.rubric import Rubric

__all__ = ["MAX_SECTION_CHARS", "MAX_SOURCE_CARDS", "WORD_TOLERANCE", "WriterAgent"]

#: The writer's own length discipline: target words ±10 % (tighter than the ±15 % gate).
WORD_TOLERANCE: float = 0.10
#: How much existing section text is shown to the model. More is not better: patches are
#: local, and a writer that receives 40k characters re-sends them in its answer.
MAX_SECTION_CHARS: int = 18_000
#: Cards shown with their snippet; the snippet is the only evidence a writer may quote.
MAX_SOURCE_CARDS: int = 12


class WriterAgent(RoleAgent):
    """Draft or revise exactly one section, citing only the cards it was given."""

    role = AgentRole.WRITER
    default_tier = ModelTier.MID
    output_model: ClassVar[type[Contract]] = WriterOutput
    #: A writer returns patches and an answer; cards and reports belong to other roles.
    forbidden_result_fields: ClassVar[tuple[str, ...]] = ("source_cards", "report")
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    max_input_tokens: ClassVar[int] = 24_000
    max_output_tokens: ClassVar[int] = 8_000

    def __init__(
        self,
        *,
        section_texts: SectionTextProvider | None = None,
        targets: SectionTargetProvider | None = None,
        rubric: Rubric | None = None,
        **kwargs: Any,
    ) -> None:
        """``section_texts`` is required for any section that already has text.

        ``targets`` decides the ±10 % window; without one, the rubric's midpoint for the
        brief's article type is used, and with neither the word check is skipped rather
        than guessed.
        """
        self._section_texts = section_texts
        self._targets = targets or (RubricSectionTargets(rubric) if rubric is not None else None)
        super().__init__(**kwargs)

    # -- scope ------------------------------------------------------------
    def check_task(self, task: Task, run_state: RunState) -> None:
        """One section per task, and that section must exist in the run."""
        super().check_task(task, run_state)
        if len(task.section_ids) != 1:
            raise RoleTaskInvalid(
                f"writer tasks own exactly one section; this task names "
                f"{len(task.section_ids)} ({list(task.section_ids)})",
                task_id=task.id,
                section_ids=list(task.section_ids),
            )
        if section_by_id(run_state, task.section_ids[0]) is None:
            raise RoleTaskInvalid(
                f"section '{task.section_ids[0]}' is not in the run state",
                task_id=task.id,
                section_id=task.section_ids[0],
            )

    # -- preparation ------------------------------------------------------
    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Show the model the section it edits, its word target and its evidence."""
        run = self._scratch(task)
        section = section_by_id(run_state, task.section_ids[0])
        assert section is not None  # check_task guarantees it
        text = await self._text(section)
        target = (
            None
            if self._targets is None
            else self._targets.target_words(section.id, brief=run_state.brief)
        )
        window = (
            None
            if target is None
            else [int(target * (1 - WORD_TOLERANCE)), int(target * (1 + WORD_TOLERANCE))]
        )

        run.json_block(
            "Section under edit",
            {
                "id": section.id,
                "title": section.title,
                "version": section.version,
                "status": section.status.value,
                "words": section.words,
                "target_words": target,
                "word_window": window,
                "may_replace_section": section.status in {SectionStatus.EMPTY, SectionStatus.DRAFT},
            },
        )
        run.block("Current section text", text if text.strip() else "(empty)")
        run.json_block(
            "Supplied SourceCards (cite with [[CITE:<id>]]; the snippet is the evidence)",
            {
                "sources": [
                    {
                        "id": card.id,
                        "title": card.title,
                        "year": card.year,
                        "venue": card.venue,
                        "authors": list(card.authors[:6]),
                        "snippet": card.snippet,
                        "verification": card.verification.value,
                    }
                    for card in supplied_source_cards(task, run_state)[:MAX_SOURCE_CARDS]
                ]
            },
        )
        run.data["text"] = text
        run.data["target_words"] = target
        return run.blocks

    async def _text(self, section: SectionState) -> str:
        """Current text of the section, from the injected provider when there is one."""
        if self._section_texts is None:
            if section.version > 0:
                raise ConfigError(
                    f"writer needs a section text provider to edit '{section.id}' "
                    f"(v{section.version}); refusing to guess the current text",
                    section_id=section.id,
                )
            return ""
        texts = await self._section_texts.section_texts([section.id])
        return str(texts.get(section.id, ""))[:MAX_SECTION_CHARS]

    # -- acceptance -------------------------------------------------------
    def output_problems(
        self,
        output: Contract,
        *,
        result: AgentResult,
        task: Task,
        run_state: RunState,
    ) -> list[str]:
        """Patch shape, citation traceability, word target, placeholder scan."""
        assert isinstance(output, WriterOutput)
        section = section_by_id(run_state, task.section_ids[0])
        assert section is not None
        problems: list[str] = []

        if output.section_id != section.id:
            problems.append(
                f"output.section_id: '{output.section_id}' is not the section this task owns "
                f"('{section.id}')"
            )

        if output.needs_user_input:
            if result.patches:
                problems.append(
                    "result.patches: a needs_user_input answer must not patch; ask for the "
                    "missing input instead of drafting around it"
                )
            if result.entry.status is not ChangeStatus.NEEDS_USER_INPUT:
                problems.append(
                    "entry.status: needs_user_input output requires entry.status='needs_user_input'"
                )
            return problems

        if result.entry.status is not ChangeStatus.OK:
            problems.append(
                f"entry.status: a written section is 'ok', got '{result.entry.status.value}'"
            )
        patches = [patch for patch in result.patches if patch.section_id == section.id]
        if len(patches) != 1:
            problems.append(
                f"result.patches: expected exactly one patch for '{section.id}', got {len(patches)}"
            )
            return problems

        patch = patches[0]
        problems.extend(self._patch_shape(section, patch.ops))
        text = str(self._scratch(task).data.get("text", ""))
        problems.extend(
            self._citation_problems(
                output, inserted_text(patch), allowed_source_ids(task, run_state)
            )
        )
        problems.extend(self._length_problems(task, section, patch, text))
        placeholders = placeholder_hits(inserted_text(patch))
        if placeholders:
            problems.append(
                f"result.patches: placeholder text {sorted(set(placeholders))} in drafted "
                "content; write the content or return needs_user_input"
            )
        return problems

    @staticmethod
    def _patch_shape(section: SectionState, ops: Sequence[PatchOp]) -> list[str]:
        """``replace_section`` first for an empty section; never for a reviewed one."""
        kinds = [op.op for op in ops]
        problems: list[str] = []
        if section.version == 0 and kinds[0] is not PatchOpType.REPLACE_SECTION:
            problems.append(
                f"result.patches[{section.id}].ops: an empty section must be filled with "
                "replace_section; a targeted op has no text to target"
            )
        if (
            section.status in {SectionStatus.REVIEWED, SectionStatus.FINAL}
            and PatchOpType.REPLACE_SECTION in kinds
        ):
            problems.append(
                f"result.patches[{section.id}].ops: '{section.id}' is {section.status.value}; "
                "edit it with replace_text/insert_after/delete_text so the reviewer sees "
                "the delta"
            )
        return problems

    @staticmethod
    def _citation_problems(output: WriterOutput, inserted: str, allowed: set[str]) -> list[str]:
        """Markers must name supplied cards, and the answer must report what it cited."""
        markers = cite_marker_ids(inserted)
        unknown = sorted({marker for marker in markers if marker not in allowed})
        problems: list[str] = []
        if unknown:
            problems.append(
                f"result.patches: [[CITE:...]] markers name unknown source ids {unknown}; "
                "cite only the cards supplied in this task"
            )
        declared = set(output.cited_source_ids)
        cited = set(markers)
        if declared != cited:
            problems.append(
                f"output.cited_source_ids: the answer claims {sorted(declared)} but the patch "
                f"cites {sorted(cited)}"
            )
        return problems

    def _length_problems(
        self, task: Task, section: SectionState, patch: Patch, text: str
    ) -> list[str]:
        """The ±10 % word window, measured on the text the patch leaves behind."""
        target = self._scratch(task).data.get("target_words")
        if not isinstance(target, int):
            return []
        result_words = estimated_words(current_text=text, current_words=section.words, patch=patch)
        low = int(target * (1 - WORD_TOLERANCE))
        high = int(target * (1 + WORD_TOLERANCE))
        if low <= result_words <= high:
            return []
        return [
            f"result.patches[{section.id}]: the section would carry {result_words} words; "
            f"the target is {target} ±10 % ({low}..{high})"
        ]
