"""QualityReport - the validator's judgement of one iteration.

Two things are structurally enforced here rather than left to convention:

* all 8 hard gates are present, keyed by the names in ``config/rubric.yaml``, so a
  missing gate can never read as "passed";
* scores are integers in 0..100 and ``Q`` is the same scale, which is what the loop
  controller's ``q_min`` / plateau / regression rules compare against.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from app.contracts.common import Contract, IssueId, NonEmptyStr, Score, SectionId, VersionedContract
from app.contracts.enums import IssueSeverity

__all__ = ["GATE_NAMES", "QualityIssue", "QualityReport"]

#: The 8 hard gates from config/rubric.yaml. Order is the reporting order.
GATE_NAMES: tuple[str, ...] = (
    "required_sections_present",
    "abstract_within_range",
    "total_words_within_tolerance",
    "citations_resolve_both_ways",
    "references_verified_or_flagged",
    "no_placeholders",
    "citation_style_matches",
    "referenced_figures_and_tables_exist",
)

CriteriaName = Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]{1,63}$")]


class QualityIssue(Contract):
    """One defect. ``section_id`` is None for whole-article issues (word totals, style)."""

    id: IssueId
    section_id: SectionId | None = None
    severity: IssueSeverity
    text: NonEmptyStr = Field(max_length=600, description="What is wrong and what would fix it.")


class QualityReport(VersionedContract):
    """Gates, weighted scores and the resulting Q for one iteration."""

    iter: int = Field(ge=0, le=10_000)
    gates: dict[str, bool] = Field(description="All 8 hard gate names -> pass/fail.")
    scores: dict[CriteriaName, Score] = Field(
        default_factory=dict, description="Soft criteria 0..100; keys come from the rubric."
    )
    Q: Score = Field(description="Weighted quality score 0..100.")
    verified_citation_ratio: float = Field(ge=0.0, le=1.0)
    issues: list[QualityIssue] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _gates_complete(self) -> QualityReport:
        missing = [name for name in GATE_NAMES if name not in self.gates]
        if missing:
            raise ValueError(
                f"every hard gate must be reported; missing {missing}. "
                "An absent gate must never be read as a pass."
            )
        unknown = sorted(set(self.gates) - set(GATE_NAMES))
        if unknown:
            raise ValueError(f"unknown gate names (see config/rubric.yaml): {unknown}")

        # Issue ids are the ledger's identity for a defect: the loop counts opens,
        # closes and churn by id, so two rows sharing one id cannot be told apart.
        issue_ids = [issue.id for issue in self.issues]
        duplicates = sorted({issue_id for issue_id in issue_ids if issue_ids.count(issue_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate issue ids in one report: {duplicates}")
        return self

    @property
    def passed_gates(self) -> list[str]:
        """Gate names that passed, in canonical order."""
        return [name for name in GATE_NAMES if self.gates[name]]

    @property
    def failed_gates(self) -> list[str]:
        """Gate names that failed, in canonical order."""
        return [name for name in GATE_NAMES if not self.gates[name]]

    @property
    def all_gates_pass(self) -> bool:
        """True when nothing structural blocks completion."""
        return not self.failed_gates

    def high_severity_issues(self) -> list[QualityIssue]:
        """Issues the loop treats as blocking."""
        return [issue for issue in self.issues if issue.severity is IssueSeverity.HIGH]
