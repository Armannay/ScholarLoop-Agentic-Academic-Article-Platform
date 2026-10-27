"""Validator: deterministic gates first, a cheap model for the subjective rest, then Q.

This is the only role that may publish a :class:`~app.contracts.quality.QualityReport`,
and the report is the loop's stop condition. So the order of work is fixed:

1. **Tools decide the eight hard gates.** ``doi_verify`` runs over the source cards
   (which is also the only sanctioned way to move a card to ``verified``),
   ``text_checks`` reports four gates, ``citation_check`` reports three plus
   ``verified_citation_ratio``, and the figures/tables gate is a local scan. A gate no
   tool assessed stays failed with an issue saying so.
2. **The cheap model answers questions, not numbers.** It returns yes/no subjective
   checks and a short rationale. It is never asked for a gate, a score or ``Q``; the
   schema it is constrained to has no field for them.
3. **Q comes from the reviewer's latest scores.** Per-section scores are averaged per
   criterion and carried forward only while the section hash still matches; a criterion
   with no fresh score falls back to the previous report, then to the baseline.
4. **Sections are settled last.** A section becomes ``final`` only when every gate
   passes, no high-severity review issue is open against it, and it has text.

Everything in steps 1, 3 and 4 is deterministic, so two validator runs over the same
article produce the same report.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from app.agents.context import RunState
from app.agents.roles.base import RoleAgent
from app.agents.roles.inputs import (
    NoReportSource,
    ReportSource,
    ReviewScoreboard,
    SectionTextProvider,
)
from app.agents.roles.quality import (
    GatePack,
    merge_scores,
    run_deterministic_gates,
    section_states_after_gates,
    weighted_q,
)
from app.agents.roles.schemas import ValidatorOutput
from app.contracts.agent import AgentResult
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, IssueSeverity, ModelTier
from app.contracts.llm import SystemBlock
from app.contracts.quality import QualityIssue, QualityReport
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.rubric import Rubric

__all__ = ["ValidatorAgent"]

#: A report may carry 200 issues; the validator forwards the most useful subset.
MAX_REPORT_ISSUES: int = 120


class ValidatorAgent(RoleAgent):
    """Set the hard gates from tools, ask a cheap model for subjective checks, score Q."""

    role = AgentRole.VALIDATOR
    default_tier = ModelTier.CHEAP
    output_model: ClassVar[type[Contract]] = ValidatorOutput
    #: The report, the section statuses and the verification updates are computed here.
    #: Leaving them out of the model schema turns "must not invent" into "cannot return".
    forbidden_result_fields: ClassVar[tuple[str, ...]] = (
        "patches",
        "source_cards",
        "sections",
        "report",
    )
    allowed_tools: ClassVar[tuple[str, ...]] = (
        "citation_check",
        "doi_verify",
        "sanitize_text",
        "text_checks",
    )
    max_input_tokens: ClassVar[int] = 12_000
    max_output_tokens: ClassVar[int] = 2_000

    def __init__(
        self,
        *,
        section_texts: SectionTextProvider | None = None,
        scoreboard: ReviewScoreboard | None = None,
        reports: ReportSource | None = None,
        rubric: Rubric | None = None,
        **kwargs: Any,
    ) -> None:
        """``section_texts`` and a tool registry are required; ``rubric`` scores Q."""
        self._section_texts = section_texts
        self._scoreboard = scoreboard or ReviewScoreboard()
        self._reports = reports or NoReportSource()
        self._rubric = rubric
        super().__init__(**kwargs)

    # -- scope ------------------------------------------------------------
    def check_task(self, task: Task, run_state: RunState) -> None:
        """The validator needs the brief, the text and the tools to mean anything."""
        super().check_task(task, run_state)
        self.require_brief(run_state)
        if self._rubric is None:
            raise ConfigError(
                "validator needs the rubric: gates, criteria and weights are configuration",
                role=self.role.value,
            )
        if self._section_texts is None:
            raise ConfigError(
                "validator needs a section text provider: the hard gates are checks over "
                "the article text",
                role=self.role.value,
            )
        if self._tools is None:
            raise ConfigError(
                "validator needs a tool registry: the hard gates come from deterministic "
                "tools, never from a model opinion",
                role=self.role.value,
            )

    # -- preparation ------------------------------------------------------
    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Run the deterministic checks and hand the cheap model their summary."""
        run = self._scratch(task)
        brief = run_state.brief
        assert brief is not None  # check_task guarantees it
        section_ids = [state.id for state in run_state.sections]
        texts = dict(await self._text_source().section_texts(section_ids))
        pack = await run_deterministic_gates(
            tools=self._tools,
            texts=texts,
            brief=brief,
            iteration=run_state.iter,
            sources=list(run_state.sources),
            project=self._project,
        )
        run.data["pack"] = pack
        run.data["texts"] = texts
        run.block(
            "Deterministic gate results (computed by tools; not your call)",
            pack.prompt_summary(),
        )
        run.json_block(
            "Reviewer scores available for Q",
            {
                "sections": [
                    {
                        "id": entry.section_id,
                        "iteration": entry.iteration,
                        "high_issues": len(entry.high_issues),
                        "scores": dict(sorted(entry.scores.items())),
                    }
                    for entry in self._fresh_scores(run_state).values()
                ]
            },
        )
        return run.blocks

    def _text_source(self) -> SectionTextProvider:
        provider = self._section_texts
        assert provider is not None  # check_task guarantees it
        return provider

    def _fresh_scores(self, run_state: RunState) -> dict[str, Any]:
        scoreboard = self._scoreboard
        assert scoreboard is not None
        return scoreboard.fresh(run_state.sections)

    # -- post-processing ---------------------------------------------------
    def postprocess(self, result: AgentResult, *, task: Task, run_state: RunState) -> AgentResult:
        """Publish the report: gates from tools, scores from the reviewer, Q computed."""
        run = self._scratch(task)
        pack = run.data.get("pack")
        if not isinstance(pack, GatePack):  # pragma: no cover - prepare always sets it
            raise ConfigError("validator produced no gate pack", role=self.role.value)
        rubric = self._rubric
        assert rubric is not None

        scores = merge_scores(
            rubric=rubric,
            sections=run_state.sections,
            scoreboard=self._scoreboard,
            previous=self._reports.latest_report(),
        )
        report = QualityReport(
            iter=run_state.iter,
            gates=dict(pack.gates),
            scores=scores,
            Q=weighted_q(scores, rubric),
            verified_citation_ratio=pack.verified_citation_ratio,
            issues=self._issues(pack, run_state),
        )
        sections = section_states_after_gates(
            sections=run_state.sections,
            pack=pack,
            high_issue_sections=self._high_issue_sections(run_state),
        )
        return AgentResult(
            entry=result.entry,
            patches=[],
            source_cards=list(pack.updated_sources),
            sections=sections,
            report=report,
        )

    def _issues(self, pack: GatePack, run_state: RunState) -> list[QualityIssue]:
        """Tool issues plus the blocking review issues, deduplicated by id."""
        issues: list[QualityIssue] = list(pack.issues)
        for section_id, entry in sorted(self._fresh_scores(run_state).items()):
            for index, text in enumerate(entry.high_issues):
                issues.append(
                    QualityIssue(
                        id=f"ISSUE-review-{section_id[:16]}-{index:02d}",
                        section_id=section_id,
                        severity=IssueSeverity.HIGH,
                        text=text[:600],
                    )
                )
        seen: set[str] = set()
        unique: list[QualityIssue] = []
        for issue in issues:
            if issue.id in seen:
                continue
            seen.add(issue.id)
            unique.append(issue)
            if len(unique) >= MAX_REPORT_ISSUES:
                break
        return unique

    def _high_issue_sections(self, run_state: RunState) -> set[str]:
        """Sections with an open high-severity review issue against the current text."""
        return {
            section_id
            for section_id, entry in self._fresh_scores(run_state).items()
            if entry.high_issues
        }

    @property
    def scoreboard(self) -> ReviewScoreboard:
        """The scoreboard this validator reads (shared with the reviewer)."""
        return self._scoreboard

    @property
    def report_source(self) -> ReportSource:
        """Where carry-forward scores come from."""
        return self._reports
