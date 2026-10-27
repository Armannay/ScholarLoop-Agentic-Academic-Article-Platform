"""What a role agent may read besides the task: text, targets, scores, prior reports.

The Step 5 runtime deliberately keeps article text out of the prompt (HARD RULE 3:
references travel, bodies do not). Three roles nevertheless must see text to do their
job - a writer edits it, a reviewer judges it, a validator runs deterministic checks
over it - so the text arrives through an injected provider instead of through the
contract envelope. The seam is a Protocol, not a storage class: the orchestrator passes
something backed by the article store, tests pass a dict, and neither the role agent nor
the runtime learns where the bytes live.

The same pattern covers the two numbers a role cannot invent:

* :class:`SectionTargetProvider` - the words a section is planned to carry. The
  architect's plan is the authority; the rubric's range is the fallback.
* :class:`ReviewScoreboard` - the reviewer's latest soft scores per section, tagged
  with the section hash they describe. The validator merges them and carries a score
  forward only while the hash still matches, which is what "carried forward for
  unchanged sections" means mechanically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.contracts.brief import Brief
from app.contracts.quality import QualityReport
from app.core.rubric import Rubric, resolve_sections

__all__ = [
    "EmptySectionTexts",
    "MappingSectionTargets",
    "MappingSectionTexts",
    "NoReportSource",
    "ReportSource",
    "ReviewScoreboard",
    "RubricSectionTargets",
    "SectionScore",
    "SectionTargetProvider",
    "SectionTextProvider",
    "StaticReportSource",
]


# ------------------------------------------------------------------------------- text
@runtime_checkable
class SectionTextProvider(Protocol):
    """Current text of the named sections. Missing ids may be answered with ``""``."""

    async def section_texts(self, section_ids: Sequence[str]) -> Mapping[str, str]:
        """Section id -> current body text."""
        ...


class MappingSectionTexts:
    """A dict-backed provider: the test fixture and the simplest integration."""

    def __init__(self, texts: Mapping[str, str] | None = None) -> None:
        self._texts: dict[str, str] = dict(texts or {})

    def set(self, section_id: str, text: str) -> None:
        """Replace one section's text (tests mutate between iterations)."""
        self._texts[section_id] = text

    async def section_texts(self, section_ids: Sequence[str]) -> Mapping[str, str]:
        """Return the stored text for each requested id; unknown ids read as empty."""
        return {section_id: self._texts.get(section_id, "") for section_id in section_ids}


class EmptySectionTexts:
    """Answers every section with an empty body: a first draft from nothing."""

    async def section_texts(self, section_ids: Sequence[str]) -> Mapping[str, str]:
        """Every requested section is empty."""
        return dict.fromkeys(section_ids, "")


# ---------------------------------------------------------------------------- targets
@runtime_checkable
class SectionTargetProvider(Protocol):
    """Words a section is planned to carry, or ``None`` when nobody planned it."""

    def target_words(self, section_id: str, *, brief: Brief | None) -> int | None:
        """Planned word count for ``section_id``."""
        ...


class MappingSectionTargets:
    """A fixed section id -> target words map (the architect's plan, in tests)."""

    def __init__(self, targets: Mapping[str, int] | None = None) -> None:
        self._targets: dict[str, int] = dict(targets or {})

    def target_words(self, section_id: str, *, brief: Brief | None) -> int | None:
        """The planned target, or ``None`` when the map has no entry."""
        _ = brief
        return self._targets.get(section_id)


class RubricSectionTargets:
    """Fallback targets from ``config/rubric.yaml``: the midpoint of each range.

    A brief's ``must_include`` items become extra sections with no upper bound; their
    target is the rubric's lower bound, which is the honest reading of "at least".
    """

    def __init__(self, rubric: Rubric) -> None:
        self._rubric = rubric

    def target_words(self, section_id: str, *, brief: Brief | None) -> int | None:
        """Planned words for ``section_id`` under the brief's article type."""
        if brief is None:
            return None
        specs = resolve_sections(
            self._rubric, brief.article_type, extra_titles=list(brief.must_include)
        )
        match = next((spec for spec in specs if spec.id == section_id), None)
        return None if match is None else int(match.target_words)


# ----------------------------------------------------------------------------- scores
@dataclass(frozen=True)
class SectionScore:
    """One section's reviewer scores, at the exact text they were given for.

    ``high_issues`` carries the blocking problems the reviewer raised, so the validator
    can refuse to settle a section without re-reading the review. Only the high-severity
    ones travel: medium and low issues are improvements, and the ledger keeps them.
    """

    section_id: str
    section_hash: str
    scores: dict[str, int]
    iteration: int
    high_issues: tuple[str, ...] = ()


class ReviewScoreboard:
    """The reviewer's latest scores per section, keyed by section hash.

    ``record`` is idempotent per section: the newest review wins, and the hash it was
    written against travels with it. The validator compares that hash with the current
    ``SectionState.hash``; a mismatch means the text moved on and the score is stale.
    """

    def __init__(self) -> None:
        self._latest: dict[str, SectionScore] = {}

    def record(
        self,
        *,
        section_id: str,
        section_hash: str,
        scores: Mapping[str, int],
        iteration: int,
        high_issues: Sequence[str] = (),
    ) -> None:
        """Store one section's scores (and blocking issues) for the hash they describe."""
        self._latest[section_id] = SectionScore(
            section_id=section_id,
            section_hash=section_hash,
            scores={name: int(value) for name, value in scores.items()},
            iteration=iteration,
            high_issues=tuple(high_issues),
        )

    def fresh(self, sections: Sequence[object]) -> dict[str, SectionScore]:
        """Scores whose hash still matches the section's current text.

        ``sections`` is any sequence of objects with ``id`` and ``hash`` attributes
        (``SectionState`` in every real call); the loose typing keeps this module free of
        a contracts import it does not otherwise need.
        """
        latest = self.latest()
        fresh: dict[str, SectionScore] = {}
        for state in sections:
            section_id = str(getattr(state, "id", ""))
            entry = latest.get(section_id)
            if entry is not None and entry.section_hash == getattr(state, "hash", None):
                fresh[section_id] = entry
        return fresh

    def latest(self) -> dict[str, SectionScore]:
        """A copy of the scoreboard, section id -> latest score."""
        return dict(self._latest)

    def for_section(self, section_id: str) -> SectionScore | None:
        """The latest score for one section, or ``None`` when it was never reviewed."""
        return self._latest.get(section_id)

    def clear(self) -> None:
        """Forget every score (a new run, or a rollback to an earlier snapshot)."""
        self._latest.clear()

    def __len__(self) -> int:
        return len(self._latest)


# ---------------------------------------------------------------------------- reports
@runtime_checkable
class ReportSource(Protocol):
    """The last QualityReport the run produced, for score carry-forward."""

    def latest_report(self) -> QualityReport | None:
        """The most recent report, or ``None`` before the first validator pass."""
        ...


class NoReportSource:
    """A run with no earlier report: every criterion starts at the baseline."""

    def latest_report(self) -> QualityReport | None:
        """Always ``None``."""
        return None


class StaticReportSource:
    """A fixed report, for tests and for replaying one iteration."""

    def __init__(self, report: QualityReport | None = None) -> None:
        self._report = report

    def set(self, report: QualityReport | None) -> None:
        """Replace the report the next validator pass will carry scores from."""
        self._report = report

    def latest_report(self) -> QualityReport | None:
        """The stored report."""
        return self._report
