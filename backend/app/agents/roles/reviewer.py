"""Reviewer: judge only the dirty sections, and never rewrite a word of them.

The reviewer is a *reader*, not an author. Three rules make that structural rather than
aspirational:

* **Only dirty sections travel.** A ``final`` section is settled until something changes
  it (any content change resets it to ``draft``), so a task that names one is refused
  before the call, and the prompt carries text for the dirty sections alone.
* **It cannot rewrite.** The model-facing envelope has no ``patches`` field at all, and
  every free-text field is word-capped: an anchor locates text (≤ 25 words), a problem
  explains it (≤ 80 words), a suggested fix hints at it (≤ 30 words). A rewritten
  paragraph does not fit in any of them.
* **Scores leave the role.** Per-section, per-criterion scores go into the run's
  :class:`~app.agents.roles.inputs.ReviewScoreboard` in ``postprocess``, tagged with the
  section hash they describe, so the validator can compute Q from them and carry a score
  forward only while the text is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.agents.context import RunState
from app.agents.roles.base import RoleAgent
from app.agents.roles.errors import RoleTaskInvalid
from app.agents.roles.inputs import ReviewScoreboard, SectionTextProvider
from app.agents.roles.schemas import ReviewerOutput, ReviewIssue
from app.agents.roles.validation import dirty_sections
from app.contracts.agent import AgentResult
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, IssueSeverity, ModelTier, SectionStatus
from app.contracts.llm import SystemBlock
from app.contracts.section import SectionState
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.rubric import Rubric

__all__ = ["MAX_SECTION_CHARS", "MAX_TOTAL_CHARS", "ReviewerAgent"]

#: One section's text in the prompt; a review is about the text, but a task is not a dump.
MAX_SECTION_CHARS: int = 8_000
#: Total section text across one reviewer task.
MAX_TOTAL_CHARS: int = 24_000


class ReviewerAgent(RoleAgent):
    """Read the dirty sections, report at most eight issues each, score the rubric."""

    role = AgentRole.REVIEWER
    default_tier = ModelTier.STRONG
    output_model: ClassVar[type[Contract]] = ReviewerOutput
    #: The reviewer must not be able to emit prose at all: no patches, no cards, no report.
    forbidden_result_fields: ClassVar[tuple[str, ...]] = (
        "patches",
        "source_cards",
        "report",
    )
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    max_input_tokens: ClassVar[int] = 24_000
    max_output_tokens: ClassVar[int] = 4_000

    def __init__(
        self,
        *,
        section_texts: SectionTextProvider | None = None,
        scoreboard: ReviewScoreboard | None = None,
        rubric: Rubric | None = None,
        **kwargs: Any,
    ) -> None:
        """``scoreboard`` is shared with the validator; ``rubric`` pins the criteria."""
        self._section_texts = section_texts
        self._scoreboard = scoreboard
        self._rubric = rubric
        super().__init__(**kwargs)

    # -- scope ------------------------------------------------------------
    def check_task(self, task: Task, run_state: RunState) -> None:
        """Only dirty sections may be read; a final section is settled work."""
        super().check_task(task, run_state)
        settled = [
            state.id
            for state in run_state.sections
            if state.id in task.section_ids and state.status is SectionStatus.FINAL
        ]
        if settled:
            raise RoleTaskInvalid(
                f"reviewer reads dirty sections only; {settled} are final. A final section "
                "comes back when its text changes, not when it is re-read",
                task_id=task.id,
                section_ids=settled,
            )
        if not self._dirty(task, run_state):
            raise RoleTaskInvalid(
                "reviewer has nothing dirty to read; scheduling a review of settled "
                "sections spends tokens to learn nothing",
                task_id=task.id,
            )
        if self._section_texts is None:
            raise ConfigError(
                "reviewer needs a section text provider: a review of text it cannot read "
                "would score an empty article",
                role=self.role.value,
            )

    def _dirty(self, task: Task, run_state: RunState) -> list[SectionState]:
        """Dirty sections this task may read, in article order."""
        return dirty_sections(task, run_state)

    # -- preparation ------------------------------------------------------
    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Give the model the dirty sections' text (and nothing else)."""
        run = self._scratch(task)
        dirty = self._dirty(task, run_state)
        texts = await self._texts([state.id for state in dirty])
        remaining = MAX_TOTAL_CHARS
        rows: list[dict[str, Any]] = []
        for state in dirty:
            text = texts.get(state.id, "")[:MAX_SECTION_CHARS][:remaining]
            remaining -= len(text)
            rows.append(
                {
                    "id": state.id,
                    "title": state.title,
                    "status": state.status.value,
                    "version": state.version,
                    "words": state.words,
                    "text": text,
                }
            )
            run.block(f"Dirty section: {state.id} ({state.status.value}, v{state.version})", text)
        run.json_block(
            "Sections to review",
            {
                "criteria": [criterion.name for criterion in self._rubric.criteria]
                if self._rubric is not None
                else [],
                "sections": [{key: row[key] for key in row if key != "text"} for row in rows],
            },
        )
        run.data["dirty"] = [state.id for state in dirty]
        return run.blocks

    async def _texts(self, section_ids: Sequence[str]) -> dict[str, str]:
        if self._section_texts is None:
            return dict.fromkeys(section_ids, "")
        return dict(await self._section_texts.section_texts(section_ids))

    # -- acceptance -------------------------------------------------------
    def output_problems(
        self,
        output: Contract,
        *,
        result: AgentResult,
        task: Task,
        run_state: RunState,
    ) -> list[str]:
        """Reviews stay in scope, statuses stay honest, scores stay complete."""
        assert isinstance(output, ReviewerOutput)
        problems: list[str] = []
        dirty = set(self._scratch(task).data.get("dirty", []))
        reviewed = {review.section_id for review in output.reviews}
        unclean = sorted(reviewed - dirty)
        if unclean:
            problems.append(
                f"output.reviews: {unclean} are not dirty sections assigned to this task "
                f"({sorted(dirty)}); the reviewer reads dirty sections only"
            )
        if task.section_ids and not set(task.section_ids) <= reviewed:
            problems.append(
                f"output.reviews: every assigned dirty section needs a review; missing "
                f"{sorted(set(task.section_ids) - reviewed)}"
            )

        problems.extend(self._score_problems(output))
        problems.extend(self._status_problems(output, result, run_state))
        return problems

    def _score_problems(self, output: ReviewerOutput) -> list[str]:
        """Scores must cover the rubric, once, 0..100 (the schema pins the range)."""
        if self._rubric is None:
            return []
        expected = {criterion.name for criterion in self._rubric.criteria}
        problems: list[str] = []
        for review in output.reviews:
            missing = sorted(expected - set(review.scores))
            unknown = sorted(set(review.scores) - expected)
            if missing:
                problems.append(
                    f"output.reviews[{review.section_id}].scores: missing rubric criteria {missing}"
                )
            if unknown:
                problems.append(
                    f"output.reviews[{review.section_id}].scores: {unknown} are not rubric "
                    f"criteria; the rubric defines {sorted(expected)}"
                )
        return problems

    def _status_problems(
        self, output: ReviewerOutput, result: AgentResult, run_state: RunState
    ) -> list[str]:
        """``reviewed`` needs zero high issues; ``final`` is never the reviewer's call."""
        problems: list[str] = []
        current = {state.id: state for state in run_state.sections}
        high_by_section = {
            review.section_id: [
                issue for issue in review.issues if issue.severity is IssueSeverity.HIGH
            ]
            for review in output.reviews
        }
        for state in result.sections:
            problems.extend(self._single_status_problem(state, current, high_by_section))
        return problems

    @staticmethod
    def _single_status_problem(
        state: SectionState,
        current: dict[str, SectionState],
        high_by_section: Mapping[str, Sequence[ReviewIssue]],
    ) -> list[str]:
        if state.status is SectionStatus.FINAL:
            return [
                f"result.sections[{state.id}].status: a reviewer never marks a section "
                "final; that is the validator's call after the gates pass"
            ]
        if state.id not in current:
            return [f"result.sections[{state.id}]: section is not in the run state"]
        if state.status is SectionStatus.REVIEWED and high_by_section.get(state.id):
            return [
                f"result.sections[{state.id}].status: 'reviewed' with a high-severity issue "
                "open; high issues block review"
            ]
        return []

    # -- post-processing ---------------------------------------------------
    def postprocess(self, result: AgentResult, *, task: Task, run_state: RunState) -> AgentResult:
        """Record the scores where the validator can find them, keyed by section hash."""
        output = self.take_output(task)
        if self._scoreboard is not None and isinstance(output, ReviewerOutput):
            hashes = {state.id: state.hash for state in run_state.sections}
            for review in output.reviews:
                section_hash = hashes.get(review.section_id)
                if section_hash is None:
                    continue
                self._scoreboard.record(
                    section_id=review.section_id,
                    section_hash=section_hash,
                    scores=review.scores,
                    iteration=run_state.iter,
                    high_issues=[
                        f"{issue.anchor}: {issue.problem}"
                        for issue in review.issues
                        if issue.severity is IssueSeverity.HIGH
                    ],
                )
        return result
