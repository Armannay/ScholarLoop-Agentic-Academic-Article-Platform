"""Role-specific structured answers, sitting next to the ``AgentResult`` envelope.

A role agent answers with **two documents in one JSON object**:

.. code-block:: json

    {"result": {"entry": {...}, "patches": [...], ...},
     "output": {"...": "role-specific payload"}}

``result`` is the frozen ``AgentResult`` contract - the ledger entry, patches, source
cards, section states and (validator only) the ``QualityReport``. ``output`` is the
model in this module: the part of a role's answer that CONTRACTS v1 has no field for
(an architect's per-section goal and claim map, a researcher's two-sentence finding, a
reviewer's per-section soft scores). The two are validated together and neither can be
skipped, so a role cannot answer with a valid envelope that says nothing, nor with a
rich payload that never reaches the ledger.

Every model is ``extra="forbid"`` (from :class:`~app.contracts.common.Contract`) and
every free-text field is word-capped. The caps are the anti-rewrite rule made
structural: a reviewer cannot smuggle a rewritten paragraph into a ``problem`` field,
because 80 words is the ceiling and a paragraph is longer.

CONTRACT-CHANGE-REQUEST CCR-7.1: these payloads would rather live *inside*
``AgentResult`` (a ``role_output`` field). See ``docs/arena-log/step-07/output.md``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.contracts.common import Contract, NonEmptyStr, Score, SectionId, Ulid
from app.contracts.enums import IssueSeverity
from app.contracts.quality import CriteriaName

__all__ = [
    "DEFAULT_SUBJECTIVE_CHECKS",
    "MAX_FINDINGS",
    "MAX_ISSUES_PER_SECTION",
    "MAX_REVIEWER_TEXT_WORDS",
    "MAX_SUGGESTED_FIX_WORDS",
    "ArchitectOutput",
    "Finding",
    "ResearcherOutput",
    "ReviewIssue",
    "ReviewerOutput",
    "SectionReview",
    "SectionSeed",
    "SubjectiveChecks",
    "ValidatorOutput",
    "WriterOutput",
    "sentence_count",
]

#: A researcher returns at most this many cards/findings per task.
MAX_FINDINGS: int = 12
#: A reviewer returns at most this many issues per section.
MAX_ISSUES_PER_SECTION: int = 8
#: The contract's "suggested fix <= 30 words" rule, enforced by the schema.
MAX_SUGGESTED_FIX_WORDS: int = 30
#: No reviewer string may exceed this many words: a paragraph is longer, by design.
MAX_REVIEWER_TEXT_WORDS: int = 80
#: Checkable subjective criteria the cheap validator model answers yes/no.
DEFAULT_SUBJECTIVE_CHECKS: tuple[str, ...] = (
    "claims_supported",
    "coherence",
    "methods_clarity",
    "results_match_claims",
    "limitations_and_ethics",
)

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def sentence_count(text: str) -> int:
    """Count sentence ends in ``text``; used to keep findings to two sentences."""
    return len(_SENTENCE_END.findall(text.strip()))


# --------------------------------------------------------------------------- architect
class SectionSeed(Contract):
    """One planned section: what it is for, how long it is, what it must establish.

    ``key_claims`` is the claims map for this section and ``required_sources`` names the
    evidence it needs. Neither can be stored in ``SectionState`` (CONTRACTS v1 holds
    id/title/order/version/hash/words/citations/status only), which is exactly why the
    architect returns them here, next to the ``SectionState`` seeds.
    """

    id: SectionId = Field(description="Rubric section id, verbatim, e.g. 'related-work'.")
    title: NonEmptyStr = Field(max_length=200)
    goal: NonEmptyStr = Field(max_length=300, description="One line: what this section must do.")
    target_words: int = Field(
        ge=0, le=20_000, description="Words this section is planned to carry."
    )
    key_claims: list[NonEmptyStr] = Field(
        default_factory=list, max_length=12, description="The claims map: what it establishes."
    )
    required_sources: list[NonEmptyStr] = Field(
        default_factory=list, max_length=12, description="Evidence this section needs."
    )

    @field_validator("key_claims", "required_sources")
    @classmethod
    def _short_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            if len(entry.split()) > 40:
                raise ValueError("claims and required sources are one-liners (<= 40 words)")
        return value


class ArchitectOutput(Contract):
    """An ordered section list. Order is list position; ids are the rubric's ids."""

    sections: list[SectionSeed] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _ids_unique(self) -> ArchitectOutput:
        ids = [section.id for section in self.sections]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise ValueError(f"duplicate section ids in the outline: {duplicates}")
        return self

    @property
    def total_target_words(self) -> int:
        """Sum of the per-section targets: the number the ±15 % gate compares."""
        return sum(section.target_words for section in self.sections)

    @property
    def ordered_ids(self) -> list[str]:
        """Section ids in planned order."""
        return [section.id for section in self.sections]


# ---------------------------------------------------------------------------- researcher
class Finding(Contract):
    """What one source says about the question: a two-sentence, quoted-support note."""

    source_id: Ulid = Field(description="Id of the SourceCard this finding belongs to.")
    finding: NonEmptyStr = Field(
        max_length=400, description="Two sentences at most: what this source establishes."
    )

    @field_validator("finding")
    @classmethod
    def _two_sentences(cls, value: str) -> str:
        if sentence_count(value) > 2:
            raise ValueError("a finding is at most two sentences")
        if len(value.split()) > 60:
            raise ValueError("a finding is at most 60 words")
        return value


class ResearcherOutput(Contract):
    """The cards' findings. ``no_results`` is how this role says "nothing was found"."""

    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    no_results: bool = False
    note: str | None = Field(
        default=None, max_length=300, description="Why nothing was found, when nothing was."
    )

    @model_validator(mode="after")
    def _no_results_is_honest(self) -> ResearcherOutput:
        if self.no_results and self.findings:
            raise ValueError(
                "no_results=true with findings present is contradictory: return the "
                "findings, or say nothing was found"
            )
        if not self.no_results and not self.findings:
            raise ValueError(
                "return at least one finding, or set no_results=true. An empty list "
                "with no_results=false hides a failed search"
            )
        return self

    @model_validator(mode="after")
    def _unique_sources(self) -> ResearcherOutput:
        ids = [finding.source_id for finding in self.findings]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise ValueError(f"one finding per source: {duplicates} appears more than once")
        return self


# ------------------------------------------------------------------------------ reviewer
class ReviewIssue(Contract):
    """One defect a writer can act on. Never a rewrite: the fix is a suggestion."""

    severity: IssueSeverity
    anchor: NonEmptyStr = Field(
        max_length=200, description="Quoted or located text the issue is about."
    )
    problem: NonEmptyStr = Field(max_length=600, description="What is wrong, and why it matters.")
    suggested_fix: NonEmptyStr = Field(
        max_length=200,
        description=f"How to fix it, in <= {MAX_SUGGESTED_FIX_WORDS} words. A hint, not prose.",
    )

    @field_validator("anchor")
    @classmethod
    def _anchor_is_a_locus(cls, value: str) -> str:
        if len(value.split()) > 25:
            raise ValueError("an anchor locates text (<= 25 words); it does not restate it")
        return value

    @field_validator("problem")
    @classmethod
    def _problem_is_short(cls, value: str) -> str:
        if len(value.split()) > MAX_REVIEWER_TEXT_WORDS:
            raise ValueError(
                f"a problem statement is at most {MAX_REVIEWER_TEXT_WORDS} words; the "
                "reviewer never returns rewritten paragraphs"
            )
        return value

    @field_validator("suggested_fix")
    @classmethod
    def _fix_is_capped(cls, value: str) -> str:
        if len(value.split()) > MAX_SUGGESTED_FIX_WORDS:
            raise ValueError(f"a suggested fix is at most {MAX_SUGGESTED_FIX_WORDS} words")
        return value


class SectionReview(Contract):
    """One dirty section, judged: at most eight issues plus the rubric scores."""

    section_id: SectionId
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=MAX_ISSUES_PER_SECTION)
    scores: dict[CriteriaName, Score] = Field(
        default_factory=dict,
        description="0..100 per rubric criterion for this section. The validator scores Q.",
    )


class ReviewerOutput(Contract):
    """Reviews of the dirty sections only. No prose, no patches, no rewrites."""

    reviews: list[SectionReview] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _one_review_per_section(self) -> ReviewerOutput:
        ids = [review.section_id for review in self.reviews]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise ValueError(f"one review per section; repeated: {duplicates}")
        return self

    def issues_by_section(self) -> dict[str, list[ReviewIssue]]:
        """Section id -> issues, for the reviewer's own status bookkeeping."""
        return {review.section_id: list(review.issues) for review in self.reviews}

    def scores_by_section(self) -> dict[str, dict[str, int]]:
        """Section id -> criterion scores, for the run scoreboard."""
        return {review.section_id: dict(review.scores) for review in self.reviews}


# ------------------------------------------------------------------------------ validator
class SubjectiveChecks(Contract):
    """The only questions the cheap model answers: subjective, yes/no, no numbers.

    Scores are not asked for here on purpose. Q is computed from the reviewer's scores
    (carried forward for unchanged sections) and the hard gates come from tools; a cheap
    model that could move Q directly would make the score unfalsifiable.
    """

    claims_supported: bool = Field(description="Every non-obvious claim cites a supplied card.")
    coherence: bool = Field(description="Sections build one argument without contradiction.")
    methods_clarity: bool = Field(description="Method and procedure are reproducible.")
    results_match_claims: bool = Field(description="Stated results follow from the evidence.")
    limitations_and_ethics: bool = Field(
        description="Limitations are honest and data-use constraints are acknowledged."
    )

    def as_dict(self) -> dict[str, bool]:
        """Criterion name -> answer, in the canonical order."""
        return {name: bool(getattr(self, name)) for name in DEFAULT_SUBJECTIVE_CHECKS}


class ValidatorOutput(Contract):
    """Subjective checks plus a short rationale. The gates and Q are not the model's."""

    subjective_checks: SubjectiveChecks
    rationale: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=8,
        description="Why the checks came out this way; one line each, <= 30 words.",
    )

    @field_validator("rationale")
    @classmethod
    def _rationale_is_short(cls, value: list[str]) -> list[str]:
        for line in value:
            if len(line.split()) > 30:
                raise ValueError("a rationale line is at most 30 words")
        return value


# --------------------------------------------------------------------------------- writer
class WriterOutput(Contract):
    """One section's answer: did it write, or does it need input?

    The text itself lives in ``AgentResult.patches`` (a patch, never a rewrite of the
    article). This document says what the patch claims: which cards it cited and, when
    it stopped, what it could not write without.
    """

    section_id: SectionId
    status: Literal["ok", "needs_user_input"]
    cited_source_ids: list[Ulid] = Field(
        default_factory=list,
        max_length=48,
        description="SourceCard ids the text cites through [[CITE:id]] markers.",
    )
    missing_inputs: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=8,
        description="What the writer needs before it can write (needs_user_input only).",
    )

    @model_validator(mode="after")
    def _status_is_consistent(self) -> WriterOutput:
        if self.status == "needs_user_input" and not self.missing_inputs:
            raise ValueError(
                "needs_user_input must name what is missing; otherwise the loop cannot "
                "ask the user anything"
            )
        if self.status == "ok" and self.missing_inputs:
            raise ValueError("status=ok with missing_inputs is contradictory")
        return self

    @property
    def needs_user_input(self) -> bool:
        """True when the writer stopped for missing data or results."""
        return self.status == "needs_user_input"
