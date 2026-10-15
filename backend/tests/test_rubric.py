"""Rubric loader: required sections per article type, the 8 hard gates, weighted criteria."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.enums import ArticleType
from app.contracts.quality import GATE_NAMES
from app.core.errors import ConfigError, RubricError
from app.core.rubric import Rubric, load_rubric, resolve_sections, slugify

pytestmark = pytest.mark.config

GATE_BLOCK = "\n".join(f"  - {{name: {name}, description: 'gated'}}" for name in GATE_NAMES)
CRITERIA_BLOCK = """\
  - {name: contribution_and_gap, weight: 20, description: 'gap'}
  - {name: coherence, weight: 20, description: 'coherence'}
  - {name: claims_supported, weight: 20, description: 'claims'}
  - {name: methods_clarity, weight: 10, description: 'methods'}
  - {name: results_match_claims, weight: 10, description: 'results'}
  - {name: literature_coverage, weight: 10, description: 'literature'}
  - {name: language, weight: 5, description: 'language'}
  - {name: consistency, weight: 5, description: 'consistency'}
"""
SECTIONS_BLOCK = """\
    abstract: {required: true, min_words: 150, max_words: 300}
    sections:
      - {id: introduction, title: Introduction, min_words: 400, max_words: 700}
      - {id: references, title: References, min_words: 0, max_words: 0}
"""
ARTICLE_TYPES_BLOCK = "".join(
    f"  {article_type.value}:\n{SECTIONS_BLOCK}" for article_type in ArticleType
)


def write_rubric(
    tmp_path: Path,
    *,
    gates: str = GATE_BLOCK,
    criteria: str = CRITERIA_BLOCK,
    sections: str | None = None,
) -> Path:
    """Write a minimal, valid rubric with the given gates/criteria/sections blocks.

    When ``sections`` is given it replaces the per-article-type block for *every*
    article type, so a single malformed section is enough to test rejection.
    """
    path = tmp_path / "rubric.yaml"
    if sections is None:
        article_types = ARTICLE_TYPES_BLOCK
    else:
        article_types = "".join(
            f"  {article_type.value}:\n{sections}" for article_type in ArticleType
        )
    path.write_text(
        f"schema_version: 1\narticle_types:\n{article_types}gates:\n{gates}\ncriteria:\n{criteria}",
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------- shipped file
def test_shipped_rubric_loads(rubric: Rubric) -> None:
    """The shipped rubric satisfies every invariant."""
    assert tuple(gate.name for gate in rubric.gates) == GATE_NAMES
    assert set(rubric.article_types) == set(ArticleType)
    assert sum(rubric.weights().values()) == pytest.approx(100.0)


def test_all_eight_gates_are_declared_and_blocking(rubric: Rubric) -> None:
    """A hard gate that is not blocking is not a hard gate."""
    assert len(rubric.gates) == 8
    assert all(gate.blocking is True for gate in rubric.gates)


def test_criteria_weights_are_the_agreed_values(rubric: Rubric) -> None:
    """The weighted distribution agreed for v1."""
    assert rubric.weights() == {
        "contribution_and_gap": 15,
        "coherence": 15,
        "claims_supported": 20,
        "methods_clarity": 10,
        "results_match_claims": 10,
        "literature_coverage": 10,
        "language": 10,
        "consistency": 5,
        "limitations_and_ethics": 5,
    }


@pytest.mark.parametrize("article_type", list(ArticleType))
def test_every_article_type_has_required_sections(
    rubric: Rubric, article_type: ArticleType
) -> None:
    """The required-sections gate can only work if every type declares sections."""
    spec = rubric.spec_for(article_type)
    assert spec.required_sections, f"{article_type.value} declares no required sections"
    assert "references" in spec.required_ids, "every type must list references explicitly"


def test_reference_sections_carry_no_prose_budget(rubric: Rubric) -> None:
    """References are a list, not prose: max_words 0 is the honest encoding."""
    for article_type in ArticleType:
        references = next(
            section
            for section in rubric.spec_for(article_type).sections
            if section.id == "references"
        )
        assert references.accepts_prose is False


def test_abstract_range_is_sane(rubric: Rubric) -> None:
    """The abstract gate needs min <= max."""
    for article_type in ArticleType:
        abstract = rubric.spec_for(article_type).abstract
        assert abstract.min_words < abstract.max_words


# --------------------------------------------------------------------- validation
def test_missing_gate_is_rejected(tmp_path: Path) -> None:
    """A missing gate would read as a pass."""
    gates = "\n".join(f"  - {{name: {name}, description: 'gated'}}" for name in GATE_NAMES[:-1])
    with pytest.raises(RubricError, match="missing hard gates"):
        load_rubric(write_rubric(tmp_path, gates=gates))


def test_unknown_gate_is_rejected(tmp_path: Path) -> None:
    """Gate names are fixed by contracts v1."""
    gates = GATE_BLOCK + "\n  - {name: vibes_check, description: 'nope'}"
    with pytest.raises(RubricError, match="unknown gate names"):
        load_rubric(write_rubric(tmp_path, gates=gates))


def test_duplicate_gate_is_rejected(tmp_path: Path) -> None:
    """Declaring a gate twice makes its pass/fail ambiguous."""
    gates = GATE_BLOCK + f"\n  - {{name: {GATE_NAMES[0]}, description: 'again'}}"
    with pytest.raises(RubricError, match="duplicate gate names"):
        load_rubric(write_rubric(tmp_path, gates=gates))


def test_weights_must_sum_to_100(tmp_path: Path) -> None:
    """A rubric that does not sum to 100 would make Q incomparable between runs."""
    criteria = CRITERIA_BLOCK.replace("weight: 20", "weight: 19", 1)
    with pytest.raises(RubricError, match="must sum to 100"):
        load_rubric(write_rubric(tmp_path, criteria=criteria))


def test_article_type_must_be_covered(tmp_path: Path) -> None:
    """Every ArticleType needs a spec; a silent gap breaks the required-sections gate."""
    path = tmp_path / "rubric.yaml"
    path.write_text(
        "schema_version: 1\narticle_types:\n  research:\n"
        + SECTIONS_BLOCK
        + f"gates:\n{GATE_BLOCK}\ncriteria:\n{CRITERIA_BLOCK}",
        encoding="utf-8",
    )
    with pytest.raises(RubricError, match="no section spec for article types"):
        load_rubric(path)


def test_duplicate_section_ids_are_rejected(tmp_path: Path) -> None:
    """Two sections with one id would make patch targeting ambiguous."""
    sections = """\
    abstract: {required: true, min_words: 150, max_words: 300}
    sections:
      - {id: introduction, title: Introduction, min_words: 10, max_words: 20}
      - {id: introduction, title: Introduction Again, min_words: 10, max_words: 20}
"""
    path = write_rubric(tmp_path, sections=sections)
    with pytest.raises(RubricError, match="duplicate section ids"):
        load_rubric(path)


def test_inverted_section_range_is_rejected(tmp_path: Path) -> None:
    """max < min is a config bug, not a range."""
    sections = SECTIONS_BLOCK.replace(
        "min_words: 400, max_words: 700", "min_words: 700, max_words: 400"
    )
    path = write_rubric(tmp_path, sections=sections)
    with pytest.raises(RubricError, match="max_words"):
        load_rubric(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    """A missing rubric must not degrade into 'no gates'."""
    with pytest.raises(ConfigError, match="not found"):
        load_rubric(tmp_path / "absent.yaml")


# ------------------------------------------------------------------ resolve_sections
def test_resolve_sections_returns_the_declared_order(rubric: Rubric) -> None:
    """Section order is the rubric's, so the article's structure is deterministic."""
    sections = resolve_sections(rubric, ArticleType.RESEARCH)
    assert [section.id for section in sections] == rubric.spec_for(
        ArticleType.RESEARCH
    ).required_ids + [
        section.id
        for section in rubric.spec_for(ArticleType.RESEARCH).sections
        if not section.required
    ]


def test_resolve_sections_appends_extra_titles_from_the_brief(rubric: Rubric) -> None:
    """'must include X' becomes an optional section, without inventing a word range."""
    sections = resolve_sections(
        rubric, ArticleType.POSITION, extra_titles=["Data Availability Statement"]
    )
    added = sections[-1]
    assert added.id == "data-availability-statement"
    assert added.title == "Data Availability Statement"
    assert added.required is False
    assert added.max_words is None and added.accepts_prose


def test_resolve_sections_does_not_duplicate_existing_sections(rubric: Rubric) -> None:
    """A must_include that names an existing section changes nothing."""
    before = resolve_sections(rubric, ArticleType.RESEARCH)
    after = resolve_sections(
        rubric, ArticleType.RESEARCH, extra_titles=["Introduction", "introduction"]
    )
    assert [section.id for section in after] == [section.id for section in before]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Related Work", "related-work"),
        ("  Discussion!  ", "discussion"),
        ("A/B (2024)", "a-b-2024"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    """Section ids derived from titles are stable and slug-shaped."""
    assert slugify(title) == expected
