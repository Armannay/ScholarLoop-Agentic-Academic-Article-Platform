"""PlannerContext - everything one Planner call may read, and nothing else.

The context is assembled by the orchestrator from *references* (HARD RULE 3): the
bounded ``claude.md`` digest, section states (never bodies), source cards (rendered
as one-line references, never snippets of evidence), the latest quality report and
the previous plan. The Planner plans and evaluates; it never needs article prose,
so article prose is structurally absent here.

``brief`` fields and ``user_feedback`` are user-supplied text and the digest is
model-produced history, so the prompt builder fences all of it as data (HARD
RULE 4). This module only carries the values; framing lives in
:mod:`app.planner.prompts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.brief import Brief
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.core.rubric import Rubric
from app.memory.digest import DigestSnapshot

__all__ = ["ABSTRACT_SECTION_ID", "PlannerContext"]

#: The rubric models the abstract outside ``sections`` (it has its own gate), but a
#: writer task still needs an addressable section id for it. ``abstract`` is that id;
#: the article store must create a section state with this id (see HANDOFF).
ABSTRACT_SECTION_ID: str = "abstract"


@dataclass(frozen=True, slots=True)
class PlannerContext:
    """Inputs for one ``plan_initial`` / ``replan`` / ``evaluate`` call.

    Attributes:
        brief: the user's intake payload; the only source of requirements.
        rubric: the loaded rubric; supplies required sections, gates and criteria
            for ``brief.article_type``.
        digest: the bounded ``claude.md`` - a :class:`DigestSnapshot`, its markdown,
            or ``None`` before the first iteration. This is the *only* history the
            Planner ever reads (ADR-0002).
        sections: current section states (version, words, status, issues) - no bodies.
        sources: source cards the Planner may reference by id. A task may only name
            ids from this set; inventing one is a repair-triggering defect
            (HARD RULE 1). The empty tuple therefore means *no sources exist yet*,
            not "unchecked".
        report: the latest :class:`QualityReport`, when one exists.
        previous_plan: the plan being replaced (required for ``replan``).
        iteration: the loop iteration this call belongs to (0 for the initial plan).
        token_budget_cap: ceiling for the sum of task budgets in the produced plan.
            ``None`` = the mode default (``run_token_cap`` for the initial plan,
            ``iteration_token_cap`` for a replan, which only schedules one iteration).
        user_feedback: free text from the user since the last iteration. Data, never
            instructions to the system prompt.
    """

    brief: Brief
    rubric: Rubric
    digest: str | DigestSnapshot | None = None
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()
    report: QualityReport | None = None
    previous_plan: Plan | None = None
    iteration: int = 0
    token_budget_cap: int | None = None
    user_feedback: str | None = None
    _digest_markdown: str | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        # DigestSnapshot -> markdown is the only normalisation the dataclass owns;
        # a frozen dataclass needs object.__setattr__ for derived state.
        object.__setattr__(self, "_digest_markdown", self._resolve_digest())

    def _resolve_digest(self) -> str | None:
        if isinstance(self.digest, DigestSnapshot):
            return self.digest.markdown
        if self.digest is not None and self.digest.strip():
            return self.digest
        return None

    @property
    def digest_markdown(self) -> str | None:
        """The digest text to embed, or ``None`` when there is no history yet."""
        return self._digest_markdown

    def known_section_ids(self) -> frozenset[str]:
        """Section ids a produced task may target.

        Rubric sections for the article type, plus every id that already has a
        section state (the architect may have added one), plus ``abstract`` when
        the rubric requires one: the abstract is gated, so it must be plannable
        even before the article store has created its state.
        """
        spec = self.rubric.spec_for(self.brief.article_type)
        known = {section.id for section in spec.sections}
        known.update(section.id for section in self.sections)
        if spec.abstract.required:
            known.add(ABSTRACT_SECTION_ID)
        return frozenset(known)

    def known_source_ids(self) -> frozenset[str]:
        """Source-card ids a produced task may reference. Never invented ids."""
        return frozenset(source.id for source in self.sources)
