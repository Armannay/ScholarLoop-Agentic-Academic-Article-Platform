"""Assembly tests: order, word counts, the reference block and the export.

The invariant under test is that the document and the numbers always describe the
same thing: the gates measure the assembled body, so a section that moved in
``order`` or a reference list that was rendered twice would both be caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.article import (
    AssembledSection,
    assemble,
    assemble_markdown,
    export_markdown,
    per_section_words,
    section_from,
    sort_sections,
    total_words,
)
from app.article.store import ArticleStore, InMemoryStorage
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import PatchOp
from app.contracts.section import SectionState
from app.core.hashing import content_hash

pytestmark = pytest.mark.article

REFERENCES = ["Smith, J. (2020). Loops. Journal of Loops.", "Nguyen, T. (2024). Tokens. Press."]


def section(section_id: str, title: str, order: int, text: str = "") -> AssembledSection:
    """An assembly view with the word count filled in from the text."""
    return section_from(
        SectionState(
            id=section_id,
            title=title,
            order=order,
            version=0 if not text else 1,
            hash=content_hash(text),
            words=len(text.split()),
            status=SectionStatus.EMPTY if not text else SectionStatus.DRAFT,
        ),
        text,
    )


def test_order_is_the_only_ordering_authority() -> None:
    """Sections assemble by ``order``, whatever order they were passed in."""
    sections = [
        section("conclusion", "Conclusion", 2, "Done."),
        section("introduction", "Introduction", 0, "Hello."),
        section("methods", "Methods", 1, "How."),
    ]
    markdown = assemble_markdown(sections)
    assert markdown.index("## Introduction") < markdown.index("## Methods")
    assert markdown.index("## Methods") < markdown.index("## Conclusion")
    assert [item.id for item in sort_sections(sections)] == [
        "introduction",
        "methods",
        "conclusion",
    ]


def test_duplicate_orders_tie_break_on_the_id() -> None:
    """Two sections claiming the same order still assemble deterministically."""
    sections = [
        section("methods", "Methods", 0, "How."),
        section("discussion", "Discussion", 0, "Why."),
    ]
    assert [item.id for item in sort_sections(sections)] == ["discussion", "methods"]
    first = assemble_markdown(sections)
    assert first == assemble_markdown(list(reversed(sections)))


def test_references_land_in_the_references_section() -> None:
    """The rubric's ``references`` section holds the list; there is exactly one block."""
    sections = [
        section("introduction", "Introduction", 0, "A claim (Smith, 2020)."),
        section("references", "References", 1),
    ]
    document = assemble(sections, title="Loops", references=REFERENCES)
    assert document.markdown.count("## References") == 1
    assert "1. Smith, J. (2020). Loops. Journal of Loops." in document.markdown
    assert "2. Nguyen, T. (2024). Tokens. Press." in document.markdown
    assert document.reference_count == 2
    assert document.markdown.startswith("# Loops\n\n## Introduction\n")
    assert document.markdown.endswith("\n")
    assert "\n\n\n" not in document.markdown


def test_references_are_appended_when_there_is_no_references_section() -> None:
    """An article without the rubric section still gets its sources listed."""
    sections = [section("introduction", "Introduction", 0, "A claim [1].")]
    document = assemble(sections, references=REFERENCES)
    assert document.markdown.endswith(
        "## References\n\n1. Smith, J. (2020). Loops. Journal of "
        "Loops.\n2. Nguyen, T. (2024). Tokens. Press.\n"
    )


def test_word_counts_are_measured_on_the_assembled_body() -> None:
    """Per-section words come from the text, and the body total excludes references."""
    sections = [
        section("introduction", "Introduction", 0, "Three little words here."),
        section("methods", "Methods", 1, "Two words"),
        section("references", "References", 2),
    ]
    document = assemble(sections, references=REFERENCES)
    assert document.words_by_section["introduction"] == 4
    assert document.words_by_section["methods"] == 2
    # The rendered list is counted in its own section, but not in the body total.
    assert document.words_by_section["references"] > 0
    assert document.body_words == 6
    assert total_words([sections[0], sections[1]]) == 6
    assert per_section_words(sections) == {"introduction": 4, "methods": 2, "references": 0}


def test_empty_sections_stay_visible_unless_asked_otherwise() -> None:
    """A draft shows the headings it has not written yet - a gap must be visible."""
    sections = [
        section("introduction", "Introduction", 0, "Hello."),
        section("methods", "Methods", 1),
    ]
    assert "## Methods" in assemble_markdown(sections)
    assert "## Methods" not in assemble_markdown(sections, include_empty=False)


def test_heading_level_is_configurable_and_bounded() -> None:
    """Depth is a knob; nonsense is an error rather than a broken document."""
    sections = [section("introduction", "Introduction", 0, "Hello.")]
    assert assemble_markdown(sections, heading_level=3).startswith("### Introduction")
    with pytest.raises(ValueError, match="heading_level"):
        assemble_markdown(sections, heading_level=7)


def test_export_writes_utf8_and_creates_parents(tmp_path: Path) -> None:
    """The export path is created, written and returned."""
    markdown = assemble_markdown([section("introduction", "Introduction", 0, "Hello.")])
    target = tmp_path / "runs" / "run-1" / "article.md"
    written = export_markdown(target, markdown)
    assert written == target
    assert target.read_text(encoding="utf-8") == markdown
    assert markdown.endswith("\n")


async def test_store_assembly_matches_the_section_order(tmp_path: Path) -> None:
    """End to end: the store's export is ordered and carries the reference list."""
    store = ArticleStore(InMemoryStorage())
    await store.create_sections(
        [_Blueprint("introduction", "Introduction"), _Blueprint("references", "References")]
    )
    await store.patch_section(
        "introduction",
        [PatchOp(op=PatchOpType.REPLACE_SECTION, text="A claim (Smith, 2020) and more words.")],
    )
    document = await store.assembled(title="Loops", references=REFERENCES)
    assert list(document.words_by_section) == ["introduction", "references"]
    assert document.words_by_section["introduction"] == 7

    path = await store.export_markdown(tmp_path / "article.md", references=REFERENCES)
    text = path.read_text(encoding="utf-8")
    assert text.index("## Introduction") < text.index("## References")
    assert "1. Smith, J. (2020). Loops. Journal of Loops." in text


class _Blueprint:
    """Minimal stand-in for ``SectionSpec`` (id + title)."""

    def __init__(self, section_id: str, title: str) -> None:
        self.id = section_id
        self.title = title
