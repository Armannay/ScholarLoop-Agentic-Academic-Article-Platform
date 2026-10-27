"""Deterministic helpers shared by the five role agents.

Everything here is a pure function over contracts: no provider call, no I/O. The role
agents use these to turn a model's answer into *checkable* statements - which citation
ids a patch mentions, how many words it would leave behind, which sections are dirty -
so that role policy is arithmetic and regexes rather than another model opinion.

The citation-marker regex is deliberately the same shape as the one in
:mod:`app.tools.citation_check` (``[[CITE:source_id]]``): the agent's guard and the
deterministic validator must agree about what counts as a citation, or a writer could
slip past one and fail the other.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from app.agents.context import RunState
from app.contracts.common import word_count
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import Patch
from app.contracts.section import SectionState
from app.contracts.source import SourceCard, normalise_doi
from app.contracts.task import Task
from app.core.rubric import DEFAULT_PLACEHOLDER_PATTERNS

__all__ = [
    "CITE_MARKER_RE",
    "allowed_source_ids",
    "cite_marker_ids",
    "dirty_sections",
    "estimated_words",
    "inserted_text",
    "is_dirty",
    "matches_known_source",
    "normalise_title",
    "placeholder_hits",
    "section_by_id",
    "supplied_source_cards",
]

#: Mirrors ``app.tools.citation_check``: a machine marker naming one source id.
CITE_MARKER_RE = re.compile(r"\[\[CITE:([^\]\r\n]{0,1000})\]\]", re.IGNORECASE)

_DEFAULT_PLACEHOLDERS = tuple(re.compile(pattern) for pattern in DEFAULT_PLACEHOLDER_PATTERNS)
_NON_WORD = re.compile(r"[^\w\s]")


def cite_marker_ids(text: str) -> list[str]:
    """Source ids cited through ``[[CITE:id]]`` markers, in order, duplicates kept."""
    return [match.group(1).strip() for match in CITE_MARKER_RE.finditer(text)]


def inserted_text(patch: Patch) -> str:
    """Text a patch *adds*: the only place a new claim or citation can appear."""
    parts: list[str] = []
    for op in patch.ops:
        if op.op is PatchOpType.REPLACE_SECTION and op.text:
            parts.append(op.text)
        elif op.op is PatchOpType.REPLACE_TEXT and op.new:
            parts.append(op.new)
        elif op.op is PatchOpType.INSERT_AFTER and op.text:
            parts.append(op.text)
    return "\n".join(parts)


def placeholder_hits(
    text: str,
    *,
    patterns: Iterable[re.Pattern[str]] = _DEFAULT_PLACEHOLDERS,
) -> list[str]:
    """Placeholder fragments (TODO, TBD, lorem ipsum ...) present in ``text``."""
    return [match.group(0) for pattern in patterns for match in pattern.finditer(text)]


def section_by_id(run_state: RunState, section_id: str) -> SectionState | None:
    """The state of one section, or ``None`` when the run does not have it."""
    return next((state for state in run_state.sections if state.id == section_id), None)


def is_dirty(state: SectionState) -> bool:
    """True when a section may still change, and therefore may still be reviewed.

    ``final`` is the only settled state: any content change resets a section to
    ``draft`` (``SectionState.advanced``), so "not final" is exactly "dirty".
    """
    return state.status is not SectionStatus.FINAL


def dirty_sections(task: Task, run_state: RunState) -> list[SectionState]:
    """Sections this task names that are still dirty, in article order.

    A task with no ``section_ids`` is scoped to *every* dirty section, which is how the
    Planner asks for a whole-article review without listing headings again.
    """
    assigned = set(task.section_ids)
    candidates = [state for state in run_state.sections if not assigned or state.id in assigned]
    return [state for state in candidates if is_dirty(state)]


def allowed_source_ids(task: Task, run_state: RunState) -> set[str]:
    """Source ids a role may cite in this task: the task's own plus the run's cards."""
    allowed = {card.id for card in run_state.sources}
    allowed.update(task.source_ids)
    return allowed


def supplied_source_cards(task: Task, run_state: RunState) -> list[SourceCard]:
    """The SourceCards behind :func:`allowed_source_ids`, in first-seen order."""
    ordered: dict[str, SourceCard] = {}
    for card in run_state.sources:
        ordered.setdefault(card.id, card)
    for source_id in task.source_ids:
        if source_id in ordered:
            continue
        match = next((card for card in run_state.sources if card.id == source_id), None)
        if match is not None:
            ordered[source_id] = match
    allowed = allowed_source_ids(task, run_state)
    return [card for source_id, card in ordered.items() if source_id in allowed]


def estimated_words(*, current_text: str, current_words: int, patch: Patch) -> int:
    """Words the section would carry after applying ``patch``.

    A whole-section replace is exact. A targeted edit is measured as
    ``current + added - removed``, which is exact for insert/delete and within a word
    for replace (word boundaries rarely land inside the replaced span). The current
    count comes from the text when it is available and from ``SectionState.words``
    otherwise, so the check works even in a text-less integration.
    """
    base = word_count(current_text) if current_text.strip() else current_words
    replace_section = next((op for op in patch.ops if op.op is PatchOpType.REPLACE_SECTION), None)
    if replace_section is not None:
        return word_count(replace_section.text or "")
    total = base
    for op in patch.ops:
        if op.op is PatchOpType.REPLACE_TEXT:
            total += word_count(op.new or "") - word_count(op.old or "")
        elif op.op is PatchOpType.INSERT_AFTER:
            total += word_count(op.text or "")
        elif op.op is PatchOpType.DELETE_TEXT:
            total -= word_count(op.old or "")
    return max(0, total)


def normalise_title(value: str) -> str:
    """Fold a title to lowercase words so two spellings compare equal."""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(_NON_WORD.sub(" ", decomposed).split())


def _doi_key(card: SourceCard) -> str | None:
    return None if card.doi is None else normalise_doi(card.doi)


def matches_known_source(card: SourceCard, known: Sequence[SourceCard]) -> bool:
    """True when ``card`` is one of ``known``: same id, same DOI, or same title+year.

    The researcher's anti-fabrication guard. An id no search returned is not evidence
    just because it is a well-formed ULID, but a card for a DOI the search *did* return
    is the same source under a different id, which is a bookkeeping difference rather
    than an invented reference.
    """
    known_ids = {item.id for item in known}
    if card.id in known_ids:
        return True
    doi = _doi_key(card)
    if doi is not None and any(_doi_key(item) == doi for item in known if item.doi):
        return True
    title = normalise_title(card.title)
    return any(normalise_title(item.title) == title and item.year == card.year for item in known)
