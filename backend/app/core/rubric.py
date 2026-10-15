"""Typed loader for ``config/rubric.yaml``: required sections, gates, criteria.

The rubric is *data*, not code, because it is the thing most likely to change per
project: a review article needs different sections than a case study, and the soft
criteria weights are editorial policy. The loader enforces the invariants that make
the rubric usable by the loop:

* every article type in ``ArticleType`` has a spec (no silent gap in the enum);
* the 8 hard gates from :data:`app.contracts.quality.GATE_NAMES` are all declared,
  exactly once - a gate that is missing reads as "passed", which would be a lie;
* soft criteria weights are positive and sum to 100.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator

from app.contracts.common import (
    Contract,
    NonEmptyList,
    NonEmptyStr,
    SectionId,
    VersionedContract,
)
from app.contracts.enums import ArticleType
from app.contracts.quality import GATE_NAMES
from app.core.errors import RubricError, format_validation_errors
from app.core.policies import load_yaml_mapping

__all__ = [
    "ArticleTypeSpec",
    "Criterion",
    "Gate",
    "Rubric",
    "SectionSpec",
    "load_rubric",
    "resolve_sections",
    "slugify",
]

WEIGHT_TOTAL: Final = 100.0
WEIGHT_TOLERANCE: Final = 0.01
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Turn a section title into a stable section id (``Related Work`` -> ``related-work``)."""
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    return slug or "section"


class SectionSpec(Contract):
    """One section of an article type: the id the whole system keys on.

    ``max_words: 0`` is the honest way to say "this section carries no prose of its
    own" (References), and ``max_words: null`` means "no upper bound" (a section added
    because the brief demanded it).
    """

    id: SectionId
    title: NonEmptyStr = Field(max_length=200)
    required: bool = True
    min_words: int = Field(default=0, ge=0, le=20_000)
    max_words: int | None = Field(default=None, ge=0, le=20_000)

    @model_validator(mode="after")
    def _range_is_sane(self) -> SectionSpec:
        if self.max_words is not None and self.max_words < self.min_words:
            raise ValueError(
                f"section '{self.id}': max_words ({self.max_words}) < min_words ({self.min_words})"
            )
        return self

    @property
    def target_words(self) -> int:
        """Midpoint of the range, or the lower bound when unbounded."""
        if self.max_words is None:
            return self.min_words
        return (self.min_words + self.max_words) // 2

    @property
    def accepts_prose(self) -> bool:
        """False for a section that holds only a reference list (or is unbounded upward)."""
        return self.max_words is None or self.max_words > 0


class AbstractSpec(Contract):
    """The abstract has its own word range: the ``abstract_within_range`` gate."""

    required: bool = True
    min_words: int = Field(default=150, ge=1, le=2_000)
    max_words: int = Field(default=300, ge=1, le=2_000)

    @model_validator(mode="after")
    def _range_is_sane(self) -> AbstractSpec:
        if self.max_words < self.min_words:
            raise ValueError(
                f"abstract max_words ({self.max_words}) < min_words ({self.min_words})"
            )
        return self


class ArticleTypeSpec(Contract):
    """Required sections and abstract range for one article type."""

    sections: NonEmptyList[SectionSpec]
    abstract: AbstractSpec = Field(default_factory=AbstractSpec)

    @model_validator(mode="after")
    def _ids_unique(self) -> ArticleTypeSpec:
        ids = [section.id for section in self.sections]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise ValueError(f"duplicate section ids: {duplicates}")
        return self

    @property
    def required_sections(self) -> list[SectionSpec]:
        """Sections that must exist before the article can be called complete."""
        return [section for section in self.sections if section.required]

    @property
    def required_ids(self) -> list[str]:
        """Ids of the required sections, in declaration order."""
        return [section.id for section in self.required_sections]


class Gate(Contract):
    """A hard gate. ``blocking`` exists so policy can be explicit, not implicit."""

    name: str = Field(min_length=2, max_length=64)
    description: NonEmptyStr = Field(max_length=400)
    blocking: Literal[True] = True


class Criterion(Contract):
    """A soft criterion with a weight in the Q average."""

    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    weight: float = Field(gt=0.0, le=100.0)
    description: NonEmptyStr = Field(max_length=400)


class Rubric(VersionedContract):
    """The full scoring rubric: sections, 8 hard gates, weighted soft criteria."""

    article_types: dict[ArticleType, ArticleTypeSpec]
    gates: NonEmptyList[Gate]
    criteria: NonEmptyList[Criterion]

    @model_validator(mode="after")
    def _every_article_type_present(self) -> Rubric:
        missing = [
            article_type.value
            for article_type in ArticleType
            if article_type not in self.article_types
        ]
        if missing:
            raise ValueError(f"no section spec for article types: {missing}")
        unknown = [key.value for key in self.article_types if key not in set(ArticleType)]
        if unknown:  # pragma: no cover - dict keys are typed, guards YAML typos
            raise ValueError(f"unknown article types: {unknown}")
        return self

    @model_validator(mode="after")
    def _gates_are_exactly_the_eight(self) -> Rubric:
        names = [gate.name for gate in self.gates]
        if len(set(names)) != len(names):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"duplicate gate names: {duplicates}")
        missing = [name for name in GATE_NAMES if name not in names]
        if missing:
            raise ValueError(
                f"missing hard gates (a missing gate would read as 'passed'): {missing}"
            )
        extra = sorted(set(names) - set(GATE_NAMES))
        if extra:
            raise ValueError(f"unknown gate names: {extra}; the set is fixed in contracts v1")
        return self

    @model_validator(mode="after")
    def _weights_sum_to_100(self) -> Rubric:
        names = [criterion.name for criterion in self.criteria]
        if len(set(names)) != len(names):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"duplicate criterion names: {duplicates}")
        total = sum(criterion.weight for criterion in self.criteria)
        if abs(total - WEIGHT_TOTAL) > WEIGHT_TOLERANCE:
            raise ValueError(f"soft criteria weights must sum to {WEIGHT_TOTAL}, got {total}")
        return self

    @property
    def gate_names(self) -> tuple[str, ...]:
        """Gate names in canonical reporting order, whatever order the file used."""
        declared = [gate.name for gate in self.gates]
        return tuple(name for name in GATE_NAMES if name in set(declared))

    def weights(self) -> dict[str, float]:
        """Criterion name -> weight."""
        return {criterion.name: criterion.weight for criterion in self.criteria}

    def spec_for(self, article_type: ArticleType) -> ArticleTypeSpec:
        """Section spec for an article type."""
        return self.article_types[article_type]


def load_rubric(path: Path) -> Rubric:
    """Load and validate ``config/rubric.yaml``.

    Raises:
        ConfigError: missing file, bad YAML, or wrong top-level shape.
        RubricError: well-formed YAML that fails the rubric invariants above.
    """
    document = load_yaml_mapping(path)
    try:
        return Rubric.model_validate(document)
    except ValidationError as exc:
        raise RubricError(
            f"invalid rubric in {path} [{format_validation_errors(exc)}]",
            errors=exc.errors(include_url=False),
        ) from exc


def resolve_sections(
    rubric: Rubric, article_type: ArticleType, *, extra_titles: list[str] | None = None
) -> list[SectionSpec]:
    """Section list for an article type, plus optional sections from ``must_include``.

    ``extra_titles`` entries are appended as non-required sections with an open upper
    bound, keeping the brief's promise ("must include a Limitations section") without
    inventing a word range nobody asked for.
    """
    spec = rubric.spec_for(article_type)
    sections = list(spec.sections)
    existing = {section.id.casefold() for section in sections} | {
        section.title.casefold() for section in sections
    }
    for title in extra_titles or []:
        if title.casefold() in existing:
            continue
        sections.append(
            SectionSpec(
                id=slugify(title)[:64],
                title=title[:200],
                required=False,
                min_words=0,
                max_words=None,
            )
        )
        existing.add(title.casefold())
    return sections
