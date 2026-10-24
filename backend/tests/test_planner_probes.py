"""The deterministic half of the evaluator: gates, compact view, opt-in deep text.

The gates are facts, so they are tested as facts: build an article state, read the gate,
assert the verdict *and* the sentence the evaluator will see. The compact view is tested
for exactly the bounds the step asked for (headings, counts, two edge sentences, at most
three excerpts of at most 300 tokens).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.enums import ArticleType, CitationStyle, IssueSeverity
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.core.rubric import Rubric
from app.planner import (
    MAX_EDGE_SENTENCE_TOKENS,
    MAX_EXCERPT_TOKENS,
    MAX_EXCERPTS,
    ArticleView,
    ProbeReport,
    build_compact_view,
    build_probe_report,
    gate_results,
    split_sentences,
)
from app.providers.base import estimate_tokens
from tests.planner_support import (
    ARTICLE_LAYOUT,
    SOURCE_A,
    SOURCE_B,
    SOURCE_C,
    article_view,
    brief_for,
    full_view,
    prose,
    quality_report,
    section,
    source,
)


def failed_gates(view: ArticleView, rubric: Rubric) -> set[str]:
    """Names of the gates that fail for ``view``."""
    return set(build_probe_report(view, rubric).failed_gates)


def test_full_article_passes_every_gate(rubric: Rubric) -> None:
    """The canonical fixture is green on all eight gates, in canonical order."""
    report = build_probe_report(full_view(brief_for(ArticleType.RESEARCH)), rubric)

    assert report.all_passed, report.failed_gates
    assert [gate.name for gate in report.gates] == list(GATE_NAMES)
    assert report.gate_map == dict.fromkeys(GATE_NAMES, True)
    assert report.verified_citation_ratio == 1.0


def test_missing_required_section_fails_the_gate(rubric: Rubric) -> None:
    """A required section that does not exist is named, not counted as empty prose."""
    layout = [item for item in ARTICLE_LAYOUT if item[0] != "results"]
    view = article_view(brief_for(ArticleType.RESEARCH), layout=layout)

    report = build_probe_report(view, rubric)

    gate = report.gate("required_sections_present")
    assert gate.passed is False
    assert "results" in gate.detail
    assert gate.evidence["missing"] == ["results"]


def test_abstract_out_of_range_fails_the_gate(rubric: Rubric) -> None:
    """The abstract range comes from the rubric, not from the model."""
    layout = [("abstract", 40)] + [item for item in ARTICLE_LAYOUT if item[0] != "abstract"]
    view = article_view(brief_for(ArticleType.RESEARCH), layout=layout)

    gate = build_probe_report(view, rubric).gate("abstract_within_range")

    assert gate.passed is False
    assert "40 words" in gate.detail


def test_missing_abstract_fails_the_gate(rubric: Rubric) -> None:
    """No abstract is a failure, never a default pass."""
    layout = [item for item in ARTICLE_LAYOUT if item[0] != "abstract"]
    view = article_view(brief_for(ArticleType.RESEARCH), layout=layout)

    gate = build_probe_report(view, rubric).gate("abstract_within_range")

    assert gate.passed is False
    assert "no abstract section" in gate.detail


def test_total_words_outside_tolerance_fails_the_gate(rubric: Rubric) -> None:
    """The +-15 percent band is computed from the brief's target."""
    brief = brief_for(ArticleType.RESEARCH)
    layout = [(section_id, 10) for section_id, _ in ARTICLE_LAYOUT]
    view = article_view(brief, layout=layout)

    gate = build_probe_report(view, rubric).gate("total_words_within_tolerance")

    assert gate.passed is False
    assert gate.evidence["low"] == brief.word_tolerance_low
    assert gate.evidence["high"] == brief.word_tolerance_high


def test_unresolved_citation_fails_both_ways_gate(rubric: Rubric) -> None:
    """A marker with no card behind it is exactly the both-ways failure."""
    view = article_view(
        brief_for(ArticleType.RESEARCH),
        citation_keys=(
            ("introduction", "(Smith, 2020)", SOURCE_A),
            ("related-work", "(Jones, 2021)", None),
        ),
    )

    gate = build_probe_report(view, rubric).gate("citations_resolve_both_ways")

    assert gate.passed is False
    assert gate.evidence["unresolved"] == 1


def test_uncited_card_fails_both_ways_gate(rubric: Rubric) -> None:
    """The reverse direction matters too: an unused card is not a reference."""
    view = article_view(
        brief_for(ArticleType.RESEARCH),
        sources=[
            source(SOURCE_A, suffix="a"),
            source(SOURCE_B, suffix="b"),
            source(SOURCE_C, suffix="c"),
        ],
    )

    gate = build_probe_report(view, rubric).gate("citations_resolve_both_ways")

    assert gate.passed is False
    assert SOURCE_C in gate.detail


def test_unverified_reference_fails_unless_flagged(rubric: Rubric) -> None:
    """``unknown`` verification passes only when the card is visibly flagged."""
    unverified = [
        source(SOURCE_A, suffix="a"),
        source(SOURCE_B, suffix="b", verified=False),
    ]
    brief = brief_for(ArticleType.RESEARCH)

    failing = build_probe_report(article_view(brief, sources=unverified), rubric).gate(
        "references_verified_or_flagged"
    )
    assert failing.passed is False
    assert SOURCE_B in failing.detail

    flagged = build_probe_report(
        article_view(brief, sources=unverified, flagged=[SOURCE_B]), rubric
    ).gate("references_verified_or_flagged")
    assert flagged.passed is True


def test_placeholder_fails_the_gate(rubric: Rubric) -> None:
    """A TODO in the body is a placeholder, wherever it hides."""
    brief = brief_for(ArticleType.RESEARCH)
    view = article_view(
        brief,
        texts={"introduction": "Sentence one. TODO: finish this paragraph later."},
    )

    gate = build_probe_report(view, rubric).gate("no_placeholders")

    assert gate.passed is False
    assert "introduction" in gate.detail


@pytest.mark.parametrize("style", [CitationStyle.APA7, CitationStyle.IEEE])
def test_citation_style_is_checked_against_the_brief(rubric: Rubric, style: CitationStyle) -> None:
    """APA7 and IEEE markers are not interchangeable."""
    marker = "(Smith, 2020)" if style is CitationStyle.APA7 else "[1]"
    article_type = ArticleType.RESEARCH if style is CitationStyle.APA7 else ArticleType.CASE_STUDY
    brief = brief_for(article_type).model_copy(update={"citation_style": style})
    view = article_view(
        brief,
        citation_keys=(("introduction", marker, SOURCE_A), ("related-work", marker, SOURCE_B)),
    )

    assert build_probe_report(view, rubric).gate("citation_style_matches").passed is True

    wrong = "[1]" if style is CitationStyle.APA7 else "(Smith, 2020)"
    mismatched = article_view(
        brief,
        citation_keys=(
            ("introduction", wrong, SOURCE_A),
            ("related-work", wrong, SOURCE_B),
        ),
    )
    assert build_probe_report(mismatched, rubric).gate("citation_style_matches").passed is False


def test_missing_references_section_fails_the_style_gate(rubric: Rubric) -> None:
    """A citation style is more than the marker: the list has to exist."""
    layout = [item for item in ARTICLE_LAYOUT if item[0] != "references"]
    view = article_view(brief_for(ArticleType.RESEARCH), layout=layout)

    gate = build_probe_report(view, rubric).gate("citation_style_matches")

    assert gate.passed is False
    assert "no references section" in gate.detail


def test_figure_reference_without_definition_fails(rubric: Rubric) -> None:
    """Referring to Figure 1 without defining it is a dangling reference."""
    brief = brief_for(ArticleType.RESEARCH)
    dangling = article_view(
        brief, texts={"results": "The trend is visible in Figure 1. It is clear."}
    )
    assert (
        build_probe_report(dangling, rubric).gate("referenced_figures_and_tables_exist").passed
        is False
    )

    defined = article_view(
        brief,
        texts={
            "results": "The trend is visible in Figure 1. It is clear.",
            "discussion": "Figure 1: quality over iterations.",
        },
    )
    assert (
        build_probe_report(defined, rubric).gate("referenced_figures_and_tables_exist").passed
        is True
    )


def test_compact_view_reports_headings_counts_and_edge_sentences(rubric: Rubric) -> None:
    """The evaluator sees references plus the first and last two sentences."""
    brief = brief_for(ArticleType.RESEARCH)
    text = "One fact. Two facts. Three facts. Four facts."
    view = article_view(brief, texts={"introduction": text})

    compact = build_compact_view(view)

    introduction = next(item for item in compact.sections if item.id == "introduction")
    assert introduction.first_sentences == ["One fact.", "Two facts."]
    assert introduction.last_sentences == ["Three facts.", "Four facts."]
    assert introduction.words == 600
    assert introduction.citations == 1
    assert any(entry.startswith("Introduction (introduction)") for entry in compact.headings)
    assert compact.total_words == sum(item.words for item in compact.sections)


def test_compact_view_uses_at_most_three_excerpts_of_300_tokens(rubric: Rubric) -> None:
    """Excerpts are bounded twice: by count and by tokens."""
    brief = brief_for(ArticleType.RESEARCH)
    long_text = " ".join(f"word{index}" for index in range(5_000))
    view = article_view(brief, texts={section_id: long_text for section_id, _ in ARTICLE_LAYOUT})

    compact = build_compact_view(view, section_scores=view_scores(view))

    assert len(compact.excerpts) == MAX_EXCERPTS
    assert [excerpt.section_id for excerpt in compact.excerpts] == [
        "methodology",
        "results",
        "introduction",
    ]
    for excerpt in compact.excerpts:
        assert excerpt.tokens <= MAX_EXCERPT_TOKENS
        assert "lowest scoring section" in excerpt.reason


def view_scores(view: ArticleView) -> dict[str, int]:
    """Per-section scores used by the excerpt test."""
    del view
    return {
        "abstract": 40,
        "introduction": 20,
        "related-work": 30,
        "methodology": 10,
        "results": 15,
        "discussion": 35,
        "limitations": 45,
        "conclusion": 50,
        "references": 60,
    }


def test_excerpt_follows_the_validator_report_when_no_scores_are_given(rubric: Rubric) -> None:
    """Without section scores the weakest section is the one with the heaviest issues."""
    brief = brief_for(ArticleType.RESEARCH)
    view = article_view(brief, texts={"results": prose(3), "introduction": prose(3)})
    report = quality_report(issues=2)

    compact = build_compact_view(view, report=report)

    assert compact.excerpts
    assert compact.excerpts[0].section_id == "introduction"
    assert "open issues" in compact.excerpts[0].reason


def test_edge_sentences_are_capped(rubric: Rubric) -> None:
    """One unpunctuated paragraph cannot smuggle its whole body into the compact view."""
    brief = brief_for(ArticleType.RESEARCH)
    paragraph = " ".join(f"word{index}" for index in range(2_000))
    view = article_view(brief, texts={"conclusion": paragraph})

    compact = build_compact_view(view)

    summary = next(item for item in compact.sections if item.id == "conclusion")
    assert summary.first_sentences and summary.last_sentences
    for sentence in [*summary.first_sentences, *summary.last_sentences]:
        assert estimate_tokens(sentence) <= MAX_EDGE_SENTENCE_TOKENS
    assert "word1999" not in " ".join(summary.first_sentences + summary.last_sentences)


def test_deep_check_is_opt_in_and_bounded(rubric: Rubric) -> None:
    """Without ``deep_check`` no article prose travels; with it, at most the budget."""
    brief = brief_for(ArticleType.RESEARCH)
    view = article_view(brief, texts={"introduction": "One fact. Two facts. Three facts."})

    quiet = build_probe_report(view, rubric)
    assert quiet.deep_text is None
    assert quiet.deep_sections == []

    shallow = build_probe_report(view, rubric, deep_check=["introduction"], deep_check_tokens=10)
    assert shallow.deep_text is not None
    assert "## Full text: introduction" in shallow.deep_text
    assert shallow.deep_sections == ["introduction"]
    assert any("truncated" in note for note in shallow.notes)

    starved = build_probe_report(view, rubric, deep_check=["introduction"], deep_check_tokens=1)
    assert starved.deep_text is None
    assert any("no budget left" in note for note in starved.notes)

    full = build_probe_report(view, rubric, deep_check=["introduction"])
    assert full.deep_text is not None
    assert "Three facts." in full.deep_text
    assert full.deep_sections == ["introduction"]


def test_deep_check_skips_empty_sections(rubric: Rubric) -> None:
    """An empty section is reported, not fabricated into the prompt."""
    view = full_view(brief_for(ArticleType.RESEARCH))

    report = build_probe_report(view, rubric, deep_check=["references"])

    assert report.deep_text is None
    assert any("skipped empty section" in note for note in report.notes)


def test_probe_report_requires_all_eight_gates(rubric: Rubric) -> None:
    """A report missing a gate cannot be constructed: absence is never a pass."""
    report = build_probe_report(full_view(brief_for(ArticleType.RESEARCH)), rubric)
    payload = report.model_dump(mode="json")
    # Eight entries again, but one gate is duplicated and another is gone: the field
    # length cannot catch this, the model validator must.
    payload["gates"] = [*payload["gates"][:-1], payload["gates"][0]]

    with pytest.raises(ValidationError, match="every hard gate must be reported"):
        ProbeReport.model_validate(payload)


def test_literal_gate_list_matches_the_contract(rubric: Rubric) -> None:
    """The probes emit the contract's gate names, in the contract's order."""
    names = [gate.name for gate in gate_results(full_view(brief_for(ArticleType.RESEARCH)), rubric)]
    assert names == list(GATE_NAMES)


def test_split_sentences_is_predictable() -> None:
    """Sentence splitting is whitespace-normalising and never invents fragments."""
    assert split_sentences("One. Two!  Three?") == ["One.", "Two!", "Three?"]
    assert split_sentences("   ") == []
    assert split_sentences("No terminator") == ["No terminator"]


def test_article_view_rejects_inconsistent_input() -> None:
    """Text without a state, or a flag for a non-existent source, is a bug."""
    brief = brief_for(ArticleType.RESEARCH)
    state = section("introduction", words=10, order=0)

    with pytest.raises(ValueError, match="no state"):
        ArticleView(brief=brief, sections=(state,), texts={"results": "text"})

    with pytest.raises(ValueError, match="not sources"):
        ArticleView(
            brief=brief,
            sections=(state,),
            texts={"introduction": "text"},
            sources=[],
            flagged_source_ids=frozenset({SOURCE_A}),
        )


def test_quality_issue_model_is_used_for_excerpt_ranking() -> None:
    """The ranking reads the report's issues; this pins the direction of that read."""
    brief = brief_for(ArticleType.RESEARCH)
    view = article_view(brief, texts={"introduction": prose(2), "results": prose(2)})
    report = QualityReport(
        iter=1,
        gates=dict.fromkeys(GATE_NAMES, True),
        scores={},
        Q=60,
        verified_citation_ratio=0.5,
        issues=[
            QualityIssue(
                id="ISSUE-results-001",
                section_id="results",
                severity=IssueSeverity.HIGH,
                text="The results paragraph contradicts the abstract.",
            )
        ],
    )

    compact = build_compact_view(view, report=report)

    assert compact.excerpts[0].section_id == "results"
