"""Strict role-specific payload schemas; AgentResult remains the runtime envelope."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.contracts.common import Contract, NonEmptyStr, SectionId
from app.contracts.enums import IssueSeverity


class SectionSeed(Contract):
    id: SectionId
    title: NonEmptyStr
    goal: NonEmptyStr
    target_words: int = Field(ge=0)
    key_claims: list[NonEmptyStr] = Field(max_length=12)
    required_sources: list[NonEmptyStr] = Field(max_length=12)


class ArchitectOutput(Contract):
    sections: list[SectionSeed] = Field(min_length=1, max_length=30)


class Finding(Contract):
    source_id: NonEmptyStr
    finding: NonEmptyStr = Field(max_length=600)


class ResearcherOutput(Contract):
    findings: list[Finding] = Field(max_length=12)
    no_results: bool = False


class ReviewIssue(Contract):
    severity: IssueSeverity
    anchor: NonEmptyStr = Field(max_length=200)
    problem: NonEmptyStr = Field(max_length=600)
    suggested_fix: NonEmptyStr = Field(max_length=180)


class SectionReview(Contract):
    section_id: SectionId
    issues: list[ReviewIssue] = Field(max_length=8)


class ReviewerOutput(Contract):
    reviews: list[SectionReview]
    scores: dict[str, int] = Field(default_factory=dict)


class ValidatorOutput(Contract):
    subjective_checks: dict[str, bool] = Field(default_factory=dict)
    rationale: list[NonEmptyStr] = Field(default_factory=list, max_length=8)


class WriterOutput(Contract):
    section_id: SectionId
    patch: dict
    status: Literal["ok", "needs_user_input"]
    missing_inputs: list[NonEmptyStr] = Field(default_factory=list, max_length=8)
