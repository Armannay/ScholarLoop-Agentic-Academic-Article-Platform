"""Assemble the article: ordered sections plus the reference list, then export.

The store keeps sections apart; this module is the only place that puts them back
together, so the export and the word counts always describe the same document.
Three rules make the output deterministic:

* ``order`` is the only ordering authority, with the section id as tie-break, so two
  sections sharing an ``order`` still assemble identically on every run;
* the reference list is *rendered* into the ``references`` section (the rubric gives
  it ``max_words: 0`` - it carries no prose of its own) rather than appended, so the
  document has exactly one reference block;
* word counts are measured on the assembled body, not on stored metadata, so a stale
  counter can never make the total-words gate lie.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.contracts.common import word_count
from app.contracts.enums import SectionStatus
from app.contracts.section import SectionState

__all__ = [
    "REFERENCE_HEADING",
    "REFERENCE_SECTION_ID",
    "AssembledArticle",
    "AssembledSection",
    "assemble",
    "assemble_markdown",
    "export_markdown",
    "per_section_words",
    "section_from",
    "sort_sections",
    "total_words",
]

#: Heading of the generated reference block when the article has no references section.
REFERENCE_HEADING: Final[str] = "References"
#: Section id the rubric reserves for the reference list (see config/rubric.yaml).
REFERENCE_SECTION_ID: Final[str] = "references"


@dataclass(frozen=True)
class AssembledSection:
    """One section as it will appear in the document."""

    id: str
    title: str
    order: int
    text: str
    words: int
    status: SectionStatus

    @property
    def is_empty(self) -> bool:
        """True when the section contributes no body text."""
        return not self.text.strip()


def section_from(state: SectionState, text: str) -> AssembledSection:
    """Build the assembly view from a contract state plus its body.

    ``words`` is recomputed from the text: the stored counter is a cache, and the
    assembled document is the thing the gates measure.
    """
    return AssembledSection(
        id=state.id,
        title=state.title,
        order=state.order,
        text=text,
        words=word_count(text),
        status=state.status,
    )


def sort_sections(sections: Sequence[AssembledSection]) -> list[AssembledSection]:
    """Order by ``order``, then by id. Deterministic even with duplicate orders."""
    return sorted(sections, key=lambda section: (section.order, section.id))


def per_section_words(sections: Sequence[AssembledSection]) -> dict[str, int]:
    """Words per section, in document order."""
    return {section.id: section.words for section in sort_sections(sections)}


def total_words(sections: Sequence[AssembledSection]) -> int:
    """Body words across the article, excluding headings and the reference list."""
    return sum(section.words for section in sections)


@dataclass(frozen=True)
class AssembledArticle:
    """The document plus the numbers the gates and the ledger quote."""

    markdown: str
    words_by_section: Mapping[str, int]
    body_words: int
    reference_count: int


def assemble(
    sections: Sequence[AssembledSection],
    *,
    title: str | None = None,
    references: Sequence[str] = (),
    heading_level: int = 2,
    include_empty: bool = True,
    reference_heading: str = REFERENCE_HEADING,
    reference_section_id: str | None = REFERENCE_SECTION_ID,
) -> AssembledArticle:
    """Assemble the full markdown document and its per-section word counts.

    Args:
        sections: sections in any order; ``order`` decides the layout.
        title: rendered as the level-1 title when given.
        references: pre-rendered reference entries, in citation order. Rendering a
            ``SourceCard`` is the citation-style adapter's job, not the assembler's.
        heading_level: ``#`` depth for section headings (2 = ``##``).
        include_empty: keep headings for sections with no body yet, so a missing
            section is visible in the draft instead of silently absent.
        reference_heading: heading used when the article has no references section.
        reference_section_id: section that receives the reference list; ``None``
            appends a standalone block.
    """
    if heading_level < 1 or heading_level > 6:
        raise ValueError(f"heading_level must be 1..6, got {heading_level}")

    ordered = sort_sections(sections)
    reference_block = _render_references(references)
    prefix = "#" * heading_level
    blocks: list[str] = []
    words: dict[str, int] = {}

    if title:
        blocks.append(f"# {title.strip()}")

    for section in ordered:
        body = section.text.strip()
        if section.id == reference_section_id and reference_block:
            body = f"{body}\n\n{reference_block}".strip() if body else reference_block
        elif section.is_empty and not include_empty:
            continue

        words[section.id] = word_count(body)
        heading = f"{prefix} {section.title}"
        blocks.append(f"{heading}\n\n{body}" if body else heading)

    reference_section_present = reference_section_id is not None and any(
        section.id == reference_section_id for section in ordered
    )
    if references and not reference_section_present:
        blocks.append(f"{prefix} {reference_heading}\n\n{reference_block}")

    markdown = "\n\n".join(blocks)
    return AssembledArticle(
        markdown=f"{markdown}\n" if markdown else "",
        words_by_section=words,
        body_words=sum(
            count for section_id, count in words.items() if section_id != reference_section_id
        ),
        reference_count=len(references),
    )


def assemble_markdown(
    sections: Sequence[AssembledSection],
    *,
    title: str | None = None,
    references: Sequence[str] = (),
    heading_level: int = 2,
    include_empty: bool = True,
    reference_heading: str = REFERENCE_HEADING,
    reference_section_id: str | None = REFERENCE_SECTION_ID,
) -> str:
    """The assembled document as a single markdown string."""
    return assemble(
        sections,
        title=title,
        references=references,
        heading_level=heading_level,
        include_empty=include_empty,
        reference_heading=reference_heading,
        reference_section_id=reference_section_id,
    ).markdown


def export_markdown(path: Path | str, markdown: str, *, encoding: str = "utf-8") -> Path:
    """Write the document to ``path`` (creating parents) and return the path used."""
    target = Path(path).expanduser()
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding=encoding)
    return target


def _render_references(references: Sequence[str]) -> str:
    """Numbered markdown list. Entries arrive pre-rendered in the citation style."""
    entries = [entry.strip() for entry in references if entry.strip()]
    return "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
