"""Deterministic probes: the hard gates and the compact article view.

Two jobs, both done without a model:

1. **Compute the 8 hard gates from ``config/rubric.yaml``.** A gate is a fact about the
   text - does every required section exist, are the citations resolved both ways, are
   there placeholders - so it is computed here and *cannot* be overridden by the Planner
   or by the validator. That is the point of the evaluator: the model scores the soft
   criteria; the code decides the gates.
2. **Build the compact article view** the evaluator reads: headings, per-section word and
   citation counts, the first and last two sentences of each section, and up to three
   excerpts of at most 300 tokens taken from the weakest sections. Full text is opt-in
   (:func:`build_probe_report`'s ``deep_check``), bounded, and never re-sent twice by the
   service (see :class:`app.planner.service.PlannerService`).

Everything is a pure function of an :class:`ArticleView`, the rubric and - for excerpt
selection only - the validator's report. No provider, no network, no wall clock.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import Field, model_validator

from app.contracts.brief import Brief
from app.contracts.common import Contract, NonEmptyStr, SectionId, word_count
from app.contracts.enums import CitationStyle, IssueSeverity, SectionStatus, VerificationStatus
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.core.rubric import Rubric, SectionSpec, resolve_sections
from app.providers.base import CHARS_PER_TOKEN, estimate_tokens

__all__ = [
    "DEFAULT_DEEP_CHECK_TOKENS",
    "MAX_EDGE_SENTENCE_TOKENS",
    "MAX_EXCERPTS",
    "MAX_EXCERPT_TOKENS",
    "ArticleView",
    "CompactArticleView",
    "Excerpt",
    "GateResult",
    "ProbeReport",
    "SectionSummary",
    "build_compact_view",
    "build_probe_report",
    "gate_results",
    "split_sentences",
]

#: At most three excerpts travel with an evaluation.
MAX_EXCERPTS: Final[int] = 3
#: Each excerpt is capped at 300 tokens (the step brief's ceiling).
MAX_EXCERPT_TOKENS: Final[int] = 300
#: An edge sentence is a *reference*: cap it so one unpunctuated paragraph cannot smuggle
#: two thousand words into the evaluator prompt through ``first_sentences``.
MAX_EDGE_SENTENCE_TOKENS: Final[int] = 60
#: First and last two sentences of every section, per the step brief.
SENTENCES_PER_EDGE: Final[int] = 2
#: Opt-in full-text budget for one deep check.
DEFAULT_DEEP_CHECK_TOKENS: Final[int] = 2_000
TRUNCATION_MARKER: Final[str] = " […]"
_ABSTRACT_IDS: Final[frozenset[str]] = frozenset({"abstract"})
_REFERENCE_IDS: Final[frozenset[str]] = frozenset({"references", "reference-list", "bibliography"})

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|FIXME|XXX+|Lorem ipsum)\b"
    r"|\[(?:citation needed|insert[^\]]*|placeholder|to be written|tbd)\]"
    r"|\{\{[^}]*\}\}",
    re.IGNORECASE,
)
_FIGURE_MENTION_RE = re.compile(r"\b(Figure|Table)\s+(\d+[A-Za-z]?)\b")
_FIGURE_DEFINITION_RE = re.compile(r"^(Figure|Table)\s+(\d+[A-Za-z]?)\s*[:.–-]", re.MULTILINE)
_MARKER_PATTERNS: Final[dict[CitationStyle, re.Pattern[str]]] = {
    CitationStyle.APA7: re.compile(
        r"^\([^()]*,\s*(?:19|20)\d{2}[a-z]?(?:,\s*p{1,2}\.\s*\d+(?:[-–]\d+)?)?\)$"
    ),
    CitationStyle.HARVARD: re.compile(
        r"^\([^(),]*\s(?:19|20)\d{2}[a-z]?(?:,\s*p\.\s*\d+(?:[-–]\d+)?)?\)$"
    ),
    CitationStyle.IEEE: re.compile(r"^\[\d+(?:\s*[-,–]\s*\d+)*\]$"),
    CitationStyle.VANCOUVER: re.compile(r"^\(\d+\)$"),
}


@dataclass(frozen=True)
class ArticleView:
    """Everything the probes read: states, text and sources.

    ``texts`` maps section id -> body. A state without text is legal (an empty section);
    a text without a state is not, because nothing would then carry its version.
    """

    brief: Brief
    sections: tuple[SectionState, ...]
    texts: Mapping[str, str] = field(default_factory=dict)
    sources: tuple[SourceCard, ...] = ()
    flagged_source_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        known = {state.id for state in self.sections}
        unknown = sorted(set(self.texts) - known)
        if unknown:
            raise ValueError(f"section text for sections with no state: {unknown}")
        source_ids = {card.id for card in self.sources}
        stray = sorted(self.flagged_source_ids - source_ids)
        if stray:
            raise ValueError(f"flagged source ids that are not sources: {stray}")

    @classmethod
    def from_sections(
        cls,
        brief: Brief,
        sections: Sequence[SectionState],
        *,
        texts: Mapping[str, str] | None = None,
        sources: Sequence[SourceCard] = (),
        flagged_source_ids: Sequence[str] = (),
    ) -> ArticleView:
        """Build a view from the orchestrator's stores."""
        return cls(
            brief=brief,
            sections=tuple(sections),
            texts=dict(texts or {}),
            sources=tuple(sources),
            flagged_source_ids=frozenset(flagged_source_ids),
        )

    def ordered(self) -> tuple[SectionState, ...]:
        """Sections in article order."""
        return tuple(sorted(self.sections, key=lambda state: (state.order, state.id)))

    def text(self, section_id: str) -> str:
        """Body of ``section_id``, or an empty string when it has none yet."""
        return self.texts.get(section_id, "")

    def words(self, state: SectionState) -> int:
        """Words in a section: the state's measure, falling back to the text."""
        if state.words > 0:
            return state.words
        return word_count(self.text(state.id))

    def by_id(self) -> dict[str, SectionState]:
        """Section states indexed by id."""
        return {state.id: state for state in self.sections}

    def cited_source_ids(self) -> set[str]:
        """Every source id a citation site points at."""
        return {
            citation.source_id
            for state in self.sections
            for citation in state.citations
            if citation.source_id is not None
        }

    def section_specs(self, rubric: Rubric) -> dict[str, SectionSpec]:
        """Rubric section specs for this view's brief, indexed by id."""
        return {
            spec.id: spec
            for spec in resolve_sections(
                rubric, self.brief.article_type, extra_titles=list(self.brief.must_include)
            )
        }


class GateResult(Contract):
    """One hard gate, its verdict and the facts behind it."""

    name: str = Field(min_length=2, max_length=64)
    passed: bool
    detail: NonEmptyStr = Field(
        max_length=600, description="One sentence: what passed, or exactly what failed."
    )
    evidence: dict[str, Any] = Field(default_factory=dict)


class SectionSummary(Contract):
    """The per-section slice of the compact view: counts and edge sentences."""

    id: SectionId
    title: NonEmptyStr = Field(max_length=200)
    words: int = Field(ge=0)
    citations: int = Field(ge=0)
    status: SectionStatus
    first_sentences: list[str] = Field(default_factory=list, max_length=SENTENCES_PER_EDGE)
    last_sentences: list[str] = Field(default_factory=list, max_length=SENTENCES_PER_EDGE)


class Excerpt(Contract):
    """A bounded piece of a weak section, with the reason it was chosen."""

    section_id: SectionId
    reason: NonEmptyStr = Field(max_length=120)
    tokens: int = Field(ge=0, le=MAX_EXCERPT_TOKENS)
    text: NonEmptyStr


class CompactArticleView(Contract):
    """The article as the evaluator sees it: references, not full text (HARD RULE 3)."""

    headings: list[str] = Field(default_factory=list, max_length=200)
    sections: list[SectionSummary] = Field(default_factory=list, max_length=200)
    excerpts: list[Excerpt] = Field(default_factory=list, max_length=MAX_EXCERPTS)
    total_words: int = Field(ge=0)

    @property
    def tokens(self) -> int:
        """Estimated tokens of the rendered view, for budgeting."""
        return estimate_tokens(self.render())

    def render(self) -> str:
        """Stable compact JSON: identical state renders byte-identically."""
        payload = {
            "headings": list(self.headings),
            "sections": [section.model_dump(mode="json") for section in self.sections],
            "excerpts": [excerpt.model_dump(mode="json") for excerpt in self.excerpts],
            "total_words": self.total_words,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class ProbeReport(Contract):
    """The deterministic half of an evaluation, plus the view it was measured on."""

    gates: list[GateResult] = Field(min_length=len(GATE_NAMES), max_length=len(GATE_NAMES))
    compact: CompactArticleView
    verified_citation_ratio: float = Field(ge=0.0, le=1.0)
    deep_text: str | None = Field(
        default=None, description="Opt-in full text for the sections named in deep_sections."
    )
    deep_sections: list[str] = Field(default_factory=list, max_length=32)
    notes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _every_gate_reported_once(self) -> ProbeReport:
        names = [gate.name for gate in self.gates]
        missing = [name for name in GATE_NAMES if name not in names]
        if missing:
            raise ValueError(f"every hard gate must be reported; missing {missing}")
        unknown = sorted(set(names) - set(GATE_NAMES))
        if unknown:
            raise ValueError(f"unknown gate names: {unknown}")
        if len(set(names)) != len(names):
            raise ValueError("a gate may be reported only once")
        return self

    @property
    def gate_map(self) -> dict[str, bool]:
        """Gate name -> pass/fail, in canonical order."""
        by_name = {gate.name: gate.passed for gate in self.gates}
        return {name: by_name[name] for name in GATE_NAMES}

    @property
    def failed_gates(self) -> list[str]:
        """Failed gate names, in canonical order."""
        return [name for name, passed in self.gate_map.items() if not passed]

    @property
    def all_passed(self) -> bool:
        """True when nothing structural blocks completion."""
        return not self.failed_gates

    def gate(self, name: str) -> GateResult:
        """The result for one gate.

        Raises:
            KeyError: ``name`` is not one of the 8 gates.
        """
        for gate in self.gates:
            if gate.name == name:
                return gate
        raise KeyError(name)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences without a tokeniser dependency.

    Abbreviation handling is deliberately shallow: the view is a summary for the
    evaluator, and a spurious split costs one extra short sentence while a missing split
    would silently hide text from the judgement.
    """
    stripped = " ".join(text.split())
    if not stripped:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip()]


def _cap_tokens(text: str, limit: int) -> tuple[str, int]:
    """Return at most ``limit`` tokens of ``text`` plus the token count used."""
    if estimate_tokens(text) <= limit:
        return text, estimate_tokens(text)
    available_chars = limit * CHARS_PER_TOKEN - len(TRUNCATION_MARKER)
    if available_chars <= 0:
        return "", 0
    cut = text[:available_chars]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    capped = cut.rstrip() + TRUNCATION_MARKER
    return capped, estimate_tokens(capped)


def build_compact_view(
    view: ArticleView,
    *,
    report: QualityReport | None = None,
    section_scores: Mapping[str, int] | None = None,
) -> CompactArticleView:
    """Headings, per-section counts, edge sentences and up to three excerpts.

    Excerpts come from the weakest sections. Weakness is read from, in order:
    ``section_scores`` when the caller has real per-section scores, otherwise the
    validator's issues (high=3, med=2, low=1), otherwise nothing - in which case the
    first non-empty sections are used and the reason says so.
    """
    ordered = view.ordered()
    summaries = [_summarise(view, state) for state in ordered]
    excerpts: list[Excerpt] = []
    for section_id, reason in _excerpt_targets(view, ordered, report, section_scores):
        text = view.text(section_id).strip()
        if not text:
            continue
        capped, used = _cap_tokens(text, MAX_EXCERPT_TOKENS)
        if not capped:
            continue
        excerpts.append(Excerpt(section_id=section_id, reason=reason, tokens=used, text=capped))
        if len(excerpts) >= MAX_EXCERPTS:
            break
    return CompactArticleView(
        headings=[f"{state.title} ({state.id})" for state in ordered],
        sections=summaries,
        excerpts=excerpts,
        total_words=sum(summary.words for summary in summaries),
    )


def _summarise(view: ArticleView, state: SectionState) -> SectionSummary:
    """One section's compact row."""
    sentences = split_sentences(view.text(state.id))
    return SectionSummary(
        id=state.id,
        title=state.title,
        words=view.words(state),
        citations=len(state.citations),
        status=state.status,
        first_sentences=[_edge_sentence(text) for text in sentences[:SENTENCES_PER_EDGE]],
        last_sentences=(
            [_edge_sentence(text) for text in sentences[-SENTENCES_PER_EDGE:]] if sentences else []
        ),
    )


def _edge_sentence(text: str) -> str:
    """One edge sentence, capped: the evaluator needs the opening, not the paragraph."""
    capped, _ = _cap_tokens(text, MAX_EDGE_SENTENCE_TOKENS)
    return capped


def _excerpt_targets(
    view: ArticleView,
    ordered: Sequence[SectionState],
    report: QualityReport | None,
    section_scores: Mapping[str, int] | None,
) -> list[tuple[str, str]]:
    """Section ids to excerpt, weakest first, with the reason for each choice."""
    if section_scores:
        scored: list[tuple[int, str]] = []
        for state in ordered:
            score = section_scores.get(state.id)
            if score is not None:
                scored.append((score, state.id))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [
            (section_id, f"lowest scoring section ({score}/100)")
            for score, section_id in scored[:MAX_EXCERPTS]
        ]

    if report is not None and report.issues:
        weights = {
            IssueSeverity.HIGH: 3,
            IssueSeverity.MED: 2,
            IssueSeverity.LOW: 1,
        }
        totals: dict[str, int] = {}
        for issue in report.issues:
            if issue.section_id is None:
                continue
            totals[issue.section_id] = totals.get(issue.section_id, 0) + weights[issue.severity]
        ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        return [
            (section_id, f"weakest section by open issues (weight {weight})")
            for section_id, weight in ranked[:MAX_EXCERPTS]
        ]

    fallback = [state.id for state in ordered if view.text(state.id).strip()][:MAX_EXCERPTS]
    return [
        (section_id, "no scores available: earliest non-empty section") for section_id in fallback
    ]


def gate_results(view: ArticleView, rubric: Rubric) -> list[GateResult]:
    """The 8 hard gates, in canonical order, computed from the text alone."""
    return [
        _gate_required_sections(view, rubric),
        _gate_abstract_range(view, rubric),
        _gate_total_words(view, rubric),
        _gate_citations_both_ways(view),
        _gate_references_verified(view),
        _gate_no_placeholders(view),
        _gate_citation_style(view),
        _gate_figures_and_tables(view),
    ]


def _gate_required_sections(view: ArticleView, rubric: Rubric) -> GateResult:
    spec = rubric.spec_for(view.brief.article_type)
    states = view.by_id()
    missing: list[str] = []
    empty: list[str] = []
    for section in spec.required_sections:
        state = states.get(section.id)
        if state is None:
            missing.append(section.id)
            continue
        if section.accepts_prose and view.words(state) == 0:
            empty.append(section.id)
    passed = not missing and not empty
    if passed:
        detail = (
            f"all {len(spec.required_sections)} required sections for this article type "
            "exist and are non-empty"
        )
    else:
        parts: list[str] = []
        if missing:
            parts.append(f"missing {missing}")
        if empty:
            parts.append(f"empty {empty}")
        detail = "required sections incomplete: " + "; ".join(parts)
    return GateResult(
        name="required_sections_present",
        passed=passed,
        detail=detail,
        evidence={
            "article_type": view.brief.article_type.value,
            "required": spec.required_ids,
            "missing": missing,
            "empty": empty,
        },
    )


def _find_abstract(view: ArticleView) -> SectionState | None:
    for state in view.ordered():
        if state.id in _ABSTRACT_IDS or state.title.casefold() == "abstract":
            return state
    return None


def _gate_abstract_range(view: ArticleView, rubric: Rubric) -> GateResult:
    spec = rubric.spec_for(view.brief.article_type).abstract
    state = _find_abstract(view)
    if state is None:
        return GateResult(
            name="abstract_within_range",
            passed=False,
            detail=(
                f"no abstract section found (expected {spec.min_words}-{spec.max_words} words)"
            ),
            evidence={"min_words": spec.min_words, "max_words": spec.max_words},
        )
    words = view.words(state)
    passed = spec.min_words <= words <= spec.max_words
    detail = (
        f"abstract is {words} words, inside {spec.min_words}-{spec.max_words}"
        if passed
        else f"abstract is {words} words; the rubric allows {spec.min_words}-{spec.max_words}"
    )
    return GateResult(
        name="abstract_within_range",
        passed=passed,
        detail=detail,
        evidence={"words": words, "min_words": spec.min_words, "max_words": spec.max_words},
    )


def _gate_total_words(view: ArticleView, rubric: Rubric) -> GateResult:
    specs = view.section_specs(rubric)
    counted: list[int] = []
    for state in view.sections:
        spec = specs.get(state.id)
        if spec is None or spec.accepts_prose:
            counted.append(view.words(state))
    total = sum(counted)
    low = view.brief.word_tolerance_low
    high = view.brief.word_tolerance_high
    passed = low <= total <= high
    detail = (
        f"{total} body words, inside the +-15 percent band {low}-{high}"
        if passed
        else f"{total} body words; the +-15 percent band is {low}-{high} for a "
        f"{view.brief.target_words}-word target"
    )
    return GateResult(
        name="total_words_within_tolerance",
        passed=passed,
        detail=detail,
        evidence={"total_words": total, "low": low, "high": high},
    )


def _gate_citations_both_ways(view: ArticleView) -> GateResult:
    unresolved = [
        citation.key
        for state in view.sections
        for citation in state.citations
        if citation.source_id is None
    ]
    known = {card.id for card in view.sources}
    cited = view.cited_source_ids()
    unknown = sorted(cited - known)
    uncited = sorted(known - cited)
    passed = not unresolved and not unknown and not uncited
    parts: list[str] = []
    if unresolved:
        parts.append(
            f"{len(unresolved)} citation marker(s) with no source card, e.g. {unresolved[:2]}"
        )
    if unknown:
        parts.append(
            f"{len(unknown)} citation(s) point at source ids that do not exist: {unknown[:2]}"
        )
    if uncited:
        parts.append(f"{len(uncited)} source card(s) are never cited, e.g. {uncited[:2]}")
    detail = (
        f"all {len(cited)} citations resolve both ways"
        if passed
        else "citations do not resolve both ways: " + "; ".join(parts)
    )
    return GateResult(
        name="citations_resolve_both_ways",
        passed=passed,
        detail=detail,
        evidence={
            "unresolved": len(unresolved),
            "unknown_source_ids": len(unknown),
            "uncited_cards": len(uncited),
        },
    )


def _gate_references_verified(view: ArticleView) -> GateResult:
    cited = view.cited_source_ids()
    cards = {card.id: card for card in view.sources}
    unverified = sorted(
        source_id
        for source_id in cited
        if source_id in cards
        and cards[source_id].verification is not VerificationStatus.VERIFIED
        and source_id not in view.flagged_source_ids
    )
    passed = not unverified
    detail = (
        f"every cited reference is verified or visibly flagged ({len(cited)} cited)"
        if passed
        else f"{len(unverified)} cited reference(s) are neither verified nor flagged: "
        f"{unverified[:2]}"
    )
    return GateResult(
        name="references_verified_or_flagged",
        passed=passed,
        detail=detail,
        evidence={"cited": len(cited), "unverified": len(unverified)},
    )


def _gate_no_placeholders(view: ArticleView) -> GateResult:
    hits: list[str] = []
    for state in view.ordered():
        for match in _PLACEHOLDER_RE.finditer(view.text(state.id)):
            hits.append(f"{state.id}: {match.group(0)[:40]}")
    passed = not hits
    detail = "no placeholders found" if passed else f"{len(hits)} placeholder(s): {hits[:3]}"
    return GateResult(
        name="no_placeholders",
        passed=passed,
        detail=detail,
        evidence={"count": len(hits), "examples": hits[:5]},
    )


def _references_section(view: ArticleView) -> SectionState | None:
    for state in view.ordered():
        if state.id in _REFERENCE_IDS or state.title.casefold() in _REFERENCE_IDS:
            return state
    return None


def _gate_citation_style(view: ArticleView) -> GateResult:
    style = view.brief.citation_style
    pattern = _MARKER_PATTERNS[style]
    markers = [citation.key for state in view.sections for citation in state.citations]
    mismatched = [marker for marker in markers if pattern.match(marker) is None]
    references = _references_section(view)
    problems: list[str] = []
    if references is None:
        problems.append("there is no references section")
    if not markers:
        problems.append("there are no in-text citation markers")
    if mismatched:
        problems.append(
            f"{len(mismatched)} marker(s) do not follow {style.value}, e.g. {mismatched[:2]}"
        )
    passed = not problems
    detail = (
        f"{len(markers)} markers follow {style.value} and the reference list exists"
        if passed
        else f"citation style {style.value} does not match: " + "; ".join(problems)
    )
    return GateResult(
        name="citation_style_matches",
        passed=passed,
        detail=detail,
        evidence={
            "style": style.value,
            "markers": len(markers),
            "mismatched": len(mismatched),
            "references_section": None if references is None else references.id,
        },
    )


def _gate_figures_and_tables(view: ArticleView) -> GateResult:
    mentions: set[tuple[str, str]] = set()
    definitions: set[tuple[str, str]] = set()
    for state in view.sections:
        text = view.text(state.id)
        mentions.update(_FIGURE_MENTION_RE.findall(text))
        definitions.update(_FIGURE_DEFINITION_RE.findall(text))
    dangling = sorted(mentions - definitions)
    unmentioned = sorted(definitions - mentions)
    passed = not dangling and not unmentioned
    parts: list[str] = []
    if dangling:
        parts.append(f"referenced but not defined: {dangling[:3]}")
    if unmentioned:
        parts.append(f"defined but never referenced: {unmentioned[:3]}")
    detail = (
        "every referenced figure/table exists and every defined one is referenced"
        if passed
        else "figures/tables inconsistent: " + "; ".join(parts)
    )
    return GateResult(
        name="referenced_figures_and_tables_exist",
        passed=passed,
        detail=detail,
        evidence={
            "referenced_not_defined": [f"{kind} {number}" for kind, number in dangling],
            "defined_not_referenced": [f"{kind} {number}" for kind, number in unmentioned],
        },
    )


def _deep_text(
    view: ArticleView, sections: Sequence[str], limit: int
) -> tuple[str | None, list[str], list[str]]:
    """Opt-in full text for ``sections``, bounded by ``limit`` tokens."""
    if not sections:
        return None, [], []
    chunks: list[str] = []
    used_sections: list[str] = []
    notes: list[str] = []
    budget = limit
    for section_id in sections:
        text = view.text(section_id).strip()
        if not text:
            notes.append(f"deep check skipped empty section '{section_id}'")
            continue
        header = f"## Full text: {section_id}\n"
        cost = estimate_tokens(header + text)
        if cost > budget:
            capped, used = _cap_tokens(text, max(1, budget - estimate_tokens(header)))
            if not capped:
                notes.append(f"deep check had no budget left for '{section_id}'")
                continue
            chunks.append(header + capped)
            notes.append(f"deep check truncated '{section_id}' to {used} tokens")
            budget -= estimate_tokens(header) + used
        else:
            chunks.append(header + text)
            budget -= cost
        used_sections.append(section_id)
        if budget <= 0:
            notes.append("deep check budget exhausted")
            break
    if not chunks:
        return None, [], notes
    return "\n\n".join(chunks), used_sections, notes


def build_probe_report(
    view: ArticleView,
    rubric: Rubric,
    *,
    report: QualityReport | None = None,
    section_scores: Mapping[str, int] | None = None,
    deep_check: Sequence[str] = (),
    deep_check_tokens: int = DEFAULT_DEEP_CHECK_TOKENS,
) -> ProbeReport:
    """Compute the gates and the compact view, and attach a bounded deep check.

    Args:
        view: the article state, its text and its sources.
        rubric: the loaded rubric; it decides the required sections and abstract range.
        report: the validator's report, used only to pick the weakest sections.
        section_scores: explicit per-section scores, preferred over issues for excerpts.
        deep_check: section ids whose full text should be included. Opt-in: an evaluation
            without this argument never sends article prose.
        deep_check_tokens: ceiling for the whole deep check.

    Raises:
        ValueError: ``deep_check_tokens`` is negative.
    """
    if deep_check_tokens < 0:
        raise ValueError(f"deep_check_tokens must be >= 0, got {deep_check_tokens}")
    gates = gate_results(view, rubric)
    compact = build_compact_view(view, report=report, section_scores=section_scores)
    deep_text, deep_sections, notes = _deep_text(view, deep_check, deep_check_tokens)
    cited = view.cited_source_ids()
    verified = {
        card.id
        for card in view.sources
        if card.verification is VerificationStatus.VERIFIED and card.id in cited
    }
    ratio = 0.0 if not cited else len(verified) / len(cited)
    return ProbeReport(
        gates=gates,
        compact=compact,
        verified_citation_ratio=ratio,
        deep_text=deep_text,
        deep_sections=deep_sections,
        notes=notes,
    )
