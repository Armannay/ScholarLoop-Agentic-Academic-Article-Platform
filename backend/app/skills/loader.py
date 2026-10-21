"""Lazy skill loader for ``skills/<name>/SKILL.md``.

A *skill* is a small, reusable piece of prompt knowledge (for example the IMRaD
section discipline or the APA 7 citation shape). Skills are **prompt assets**, so
they obey the same budget logic as ``rules/``: a skill that quietly grows taxes every
call that selects it (HARD RULE 3).

The loader is deliberately lazy and deterministic:

* :meth:`SkillLoader.index` reads front matter only - never bodies - and returns
  names plus descriptions. That compact index is the only skill text that belongs in
  the *stable* prompt prefix.
* :meth:`SkillLoader.load` reads and caches one body on first request.
* :meth:`SkillLoader.select` ranks role-compatible skills by matched triggers, with
  the skill name as the tie-breaker, so filesystem enumeration order can never change
  a prompt. It selects at most :data:`MAX_SKILLS_PER_TASK` skills and respects a
  token budget.
* :meth:`SkillLoader.inspect` is the tolerant audit used by the lint command: it
  reports every malformed or oversized skill instead of stopping at the first.

Token counting uses the project's documented approximation
(:func:`app.providers.base.estimate_tokens`, ``ceil(chars / 4)``). It is **not** a
vendor tokeniser; it is deterministic, free and good enough for budgets, which is
exactly why it is used everywhere else in ScholarLoop.

Front matter (YAML, required)::

    ---
    name: imrad-structure
    description: IMRaD section order, purpose and length discipline.
    roles: [architect, writer, reviewer]
    max_tokens: 400
    triggers: [imrad, structure, outline]
    ---
    ...body...

``description`` is capped at 20 words because it travels in the index.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import Field, ValidationError, field_validator

from app.contracts.common import Contract, NonEmptyList, NonEmptyStr, word_bounded
from app.contracts.enums import AgentRole
from app.contracts.task import Task
from app.core.errors import ScholarLoopError, format_validation_errors
from app.providers.base import estimate_tokens

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "DEFAULT_SKILL_BUDGET_TOKENS",
    "MAX_DESCRIPTION_WORDS",
    "MAX_SKILLS_PER_TASK",
    "MAX_SKILL_TOKENS",
    "STARTER_SKILL_NAMES",
    "STARTER_SKILL_TOKEN_CEILING",
    "Skill",
    "SkillAudit",
    "SkillBodyTooLarge",
    "SkillError",
    "SkillIndexEntry",
    "SkillInspection",
    "SkillLoader",
    "SkillMeta",
    "SkillMetadataError",
    "SkillNotFound",
    "SkillProblem",
    "SkillSelection",
    "default_skills_dir",
    "lint_skills",
]

#: Repository-relative location of the shipped skills (``<repo>/skills``).
DEFAULT_SKILLS_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "skills"

#: Hard ceiling from the step brief: no task may select more than two skills.
MAX_SKILLS_PER_TASK: Final[int] = 2
#: Default per-task skill budget. Two starter skills at the 400-token ceiling.
DEFAULT_SKILL_BUDGET_TOKENS: Final[int] = 400 * MAX_SKILLS_PER_TASK
#: Description limit that keeps the index cheap.
MAX_DESCRIPTION_WORDS: Final[int] = 20
#: Sanity ceiling for a single skill declaration.
MAX_SKILL_TOKENS: Final[int] = 20_000
#: Project-wide ceiling every shipped skill must respect.
STARTER_SKILL_TOKEN_CEILING: Final[int] = 400
#: The seven skills this step ships; the lint command fails if one is missing.
STARTER_SKILL_NAMES: Final[tuple[str, ...]] = (
    "abstract-writing",
    "citation-apa7",
    "citation-ieee",
    "imrad-structure",
    "literature-matrix",
    "patch-format",
    "peer-review-checklist",
)

_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"
_TRIGGER_PATTERN = r"^[a-z0-9][a-z0-9 _-]{1,63}$"
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<meta>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HEADER_END = "---"
_MAX_HEADER_LINES = 200

Description = Annotated[str, word_bounded(MAX_DESCRIPTION_WORDS), Field(max_length=240)]


class SkillError(ScholarLoopError):
    """Base class for every skill-loading failure."""

    code = "skill_error"
    http_status = 500


class SkillNotFound(SkillError):
    """No installed skill carries the requested name."""

    code = "skill_not_found"
    http_status = 404


class SkillMetadataError(SkillError):
    """Front matter is missing, malformed, duplicated or inconsistent with the path."""

    code = "skill_metadata_error"


class SkillBodyTooLarge(SkillError):
    """A skill body exceeds the ``max_tokens`` its own front matter declared."""

    code = "skill_body_too_large"


class SkillMeta(Contract):
    """Validated front matter. Unknown keys are an error (a typo is not a feature)."""

    name: NonEmptyStr = Field(pattern=_NAME_PATTERN)
    description: Description
    roles: NonEmptyList[AgentRole] = Field(description="Roles allowed to select this skill.")
    max_tokens: int = Field(
        ge=1, le=MAX_SKILL_TOKENS, description="Maximum allowed skill-content tokens."
    )
    triggers: NonEmptyList[NonEmptyStr] = Field(
        max_length=16, description="Task words/phrases that make this skill relevant."
    )

    @field_validator("triggers")
    @classmethod
    def _trigger_shapes(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        for value in values:
            if not re.match(_TRIGGER_PATTERN, value):
                raise ValueError(
                    f"trigger {value!r} must be lower-case words, digits, spaces, '_' or '-'"
                )
            if value in seen:
                raise ValueError(f"duplicate trigger {value!r}")
            seen.add(value)
        return values

    @property
    def roles_label(self) -> str:
        """Comma-separated role list, for error messages."""
        return ", ".join(role.value for role in self.roles)


@dataclass(frozen=True)
class SkillIndexEntry:
    """The compact index entry: a name and a one-line description, nothing else."""

    name: str
    description: str


@dataclass(frozen=True)
class Skill:
    """One loaded skill: metadata, body and the body's token estimate."""

    meta: SkillMeta
    body: str
    tokens: int
    path: Path

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def description(self) -> str:
        return self.meta.description

    @property
    def roles(self) -> tuple[AgentRole, ...]:
        return tuple(self.meta.roles)

    @property
    def triggers(self) -> tuple[str, ...]:
        return tuple(self.meta.triggers)

    @property
    def max_tokens(self) -> int:
        return self.meta.max_tokens


@dataclass(frozen=True)
class SkillSelection:
    """The result of deterministic selection for one task."""

    skills: tuple[Skill, ...] = ()
    matched: tuple[tuple[str, tuple[str, ...]], ...] = ()
    tokens: int = 0
    budget: int = 0

    @property
    def names(self) -> tuple[str, ...]:
        """Selected skill names, in selection order."""
        return tuple(skill.name for skill in self.skills)

    def triggers_for(self, name: str) -> tuple[str, ...]:
        """Triggers that caused ``name`` to be selected."""
        return next((triggers for skill, triggers in self.matched if skill == name), ())

    def render(self) -> str:
        """Skill bodies as one prompt section, or an empty string when nothing matched."""
        if not self.skills:
            return ""
        return "\n\n".join(f"## Skill: {skill.name}\n{skill.body}" for skill in self.skills)


@dataclass(frozen=True)
class SkillProblem:
    """One lint finding, addressable by skill name and stable code."""

    skill: str
    code: str
    message: str

    def render(self) -> str:
        """One-line report, used by the CLI and by tests."""
        subject = self.skill or "<skills-dir>"
        return f"{subject}: {self.code}: {self.message}"


@dataclass(frozen=True)
class SkillAudit:
    """Measured size of one skill, for the lint summary."""

    name: str
    tokens: int
    declared_max_tokens: int
    path: Path


@dataclass(frozen=True)
class SkillInspection:
    """Tolerant audit output: what was measured and what is wrong."""

    audits: tuple[SkillAudit, ...] = ()
    problems: tuple[SkillProblem, ...] = ()


@dataclass(frozen=True)
class _IndexedSkill:
    """Front matter plus the path it came from (no body read yet)."""

    meta: SkillMeta
    path: Path


def default_skills_dir() -> Path:
    """The shipped ``skills/`` directory, resolved relative to this package."""
    return DEFAULT_SKILLS_DIR


def _parse_meta(block: str, *, path: Path) -> SkillMeta:
    """Parse and validate one YAML front-matter block, naming the file on failure."""
    try:
        raw = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise SkillMetadataError(f"{path}: front matter is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SkillMetadataError(f"{path}: front matter must be a YAML mapping")
    try:
        return SkillMeta.model_validate(dict(raw))
    except ValidationError as exc:
        raise SkillMetadataError(f"{path}: {format_validation_errors(exc)}") from exc


def _read_front_matter(path: Path) -> str:
    """Read only the front-matter block; the body is not touched (laziness)."""
    if not path.exists():
        raise SkillMetadataError(f"{path}: SKILL.md does not exist")
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
        if first.strip() != _HEADER_END:
            raise SkillMetadataError(f"{path}: SKILL.md must start with a '---' front-matter block")
        lines: list[str] = []
        for _ in range(_MAX_HEADER_LINES):
            line = handle.readline()
            if line == "":
                raise SkillMetadataError(
                    f"{path}: unterminated front matter (missing closing '---')"
                )
            if line.strip() == _HEADER_END:
                return "\n".join(lines)
            lines.append(line.rstrip("\n"))
    raise SkillMetadataError(f"{path}: front matter exceeds {_MAX_HEADER_LINES} lines")


def _read_meta(path: Path) -> SkillMeta:
    """Front matter only, validated."""
    return _parse_meta(_read_front_matter(path), path=path)


def _split_document(text: str, *, path: Path) -> tuple[SkillMeta, str]:
    """Split a full SKILL.md into validated metadata and a non-empty body."""
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        raise SkillMetadataError(f"{path}: SKILL.md must start with a '---' front-matter block")
    meta = _parse_meta(match.group("meta"), path=path)
    body = text[match.end() :].strip()
    if not body:
        raise SkillMetadataError(f"{path}: skill body is empty")
    return meta, body


def _normalise(text: str) -> str:
    """Lower-case and collapse punctuation/underscores/hyphens to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _task_text(task: Task) -> str:
    """Everything about a task that may legitimately trigger a skill."""
    parts = [task.title, task.instructions, *task.acceptance, *task.section_ids]
    return _normalise(" ".join(parts))


def _matched_triggers(triggers: Sequence[str], haystack: str) -> tuple[str, ...]:
    """Triggers present in the task text, in declaration order, without duplicates."""
    tokens = set(haystack.split())
    matched: list[str] = []
    for trigger in triggers:
        normalised = _normalise(trigger)
        if not normalised:
            continue
        if " " in normalised:
            if f" {normalised} " in f" {haystack} ":
                matched.append(trigger)
        elif normalised in tokens:
            matched.append(trigger)
    return tuple(matched)


class SkillLoader:
    """Lazy, deterministic access to the skills directory.

    Args:
        skills_dir: directory containing ``<name>/SKILL.md`` files.
        max_skills_per_task: hard selection cap, at most
            :data:`MAX_SKILLS_PER_TASK`.
        token_budget: default per-task budget for the selected bodies.
    """

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        *,
        max_skills_per_task: int = MAX_SKILLS_PER_TASK,
        token_budget: int = DEFAULT_SKILL_BUDGET_TOKENS,
    ) -> None:
        if not 1 <= max_skills_per_task <= MAX_SKILLS_PER_TASK:
            raise ValueError(
                f"max_skills_per_task must be 1..{MAX_SKILLS_PER_TASK}, got {max_skills_per_task}"
            )
        if token_budget < 1:
            raise ValueError(f"token_budget must be positive, got {token_budget}")
        self._skills_dir = Path(skills_dir) if skills_dir is not None else default_skills_dir()
        self._max_skills = max_skills_per_task
        self._token_budget = token_budget
        self._index: tuple[_IndexedSkill, ...] | None = None
        self._by_name: dict[str, _IndexedSkill] = {}
        self._bodies: dict[str, Skill] = {}
        self._load_order: list[str] = []

    @property
    def skills_dir(self) -> Path:
        """Directory this loader reads from."""
        return self._skills_dir

    @property
    def max_skills_per_task(self) -> int:
        """Selection cap for one task."""
        return self._max_skills

    @property
    def token_budget(self) -> int:
        """Default per-task body budget."""
        return self._token_budget

    def _scan(self) -> tuple[_IndexedSkill, ...]:
        """Read front matter for every skill once, sorted by skill name."""
        if self._index is not None:
            return self._index
        if not self._skills_dir.is_dir():
            raise SkillError(f"skills directory not found: {self._skills_dir}")
        entries: list[_IndexedSkill] = []
        by_name: dict[str, _IndexedSkill] = {}
        for path in sorted(self._skills_dir.glob("*/SKILL.md")):
            meta = _read_meta(path)
            directory = path.parent.name
            if meta.name != directory:
                raise SkillMetadataError(
                    f"{path}: front-matter name '{meta.name}' does not match directory "
                    f"'{directory}'"
                )
            if meta.name in by_name:
                raise SkillMetadataError(
                    f"{path}: duplicate skill name '{meta.name}' (also {by_name[meta.name].path})"
                )
            indexed = _IndexedSkill(meta=meta, path=path)
            by_name[meta.name] = indexed
            entries.append(indexed)
        entries.sort(key=lambda item: item.meta.name)
        # Commit only after every file parsed: a failed scan must leave the loader
        # exactly as it was, so a corrected directory can be retried.
        self._by_name = by_name
        self._index = tuple(entries)
        return self._index

    def index(self) -> tuple[SkillIndexEntry, ...]:
        """Names and descriptions only - the stable-prefix part of the skills system."""
        return tuple(
            SkillIndexEntry(name=entry.meta.name, description=entry.meta.description)
            for entry in self._scan()
        )

    def names(self) -> tuple[str, ...]:
        """Installed skill names, sorted."""
        return tuple(entry.meta.name for entry in self._scan())

    def render_index(self) -> str:
        """The index as a deterministic markdown list."""
        entries = self.index()
        if not entries:
            return "No skills are installed."
        return "\n".join(f"- {entry.name}: {entry.description}" for entry in entries)

    def metadata(self, name: str) -> SkillMeta:
        """Front matter for one skill; raises :class:`SkillNotFound`."""
        return self._entry(name).meta

    def _entry(self, name: str) -> _IndexedSkill:
        self._scan()
        entry = self._by_name.get(name)
        if entry is None:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            raise SkillNotFound(f"unknown skill {name!r}; installed: {known}")
        return entry

    def load(self, name: str) -> Skill:
        """Load and cache one skill body, enforcing its declared ``max_tokens``."""
        entry = self._entry(name)
        cached = self._bodies.get(name)
        if cached is not None:
            return cached
        meta, body = _split_document(entry.path.read_text(encoding="utf-8"), path=entry.path)
        if meta != entry.meta:
            raise SkillMetadataError(
                f"{entry.path}: front matter changed between index and load; rebuild the loader"
            )
        tokens = estimate_tokens(body)
        if tokens > meta.max_tokens:
            raise SkillBodyTooLarge(
                f"skill '{name}' body is {tokens} tokens; declared max_tokens is "
                f"{meta.max_tokens} ({entry.path})",
                skill=name,
                tokens=tokens,
                max_tokens=meta.max_tokens,
            )
        skill = Skill(meta=meta, body=body, tokens=tokens, path=entry.path)
        self._bodies[name] = skill
        self._load_order.append(name)
        return skill

    def loaded_names(self) -> tuple[str, ...]:
        """Names whose bodies have actually been read, in load order.

        Exposed so a test can prove that :meth:`index` stays lazy and that
        :meth:`select` loads only what it selects.
        """
        return tuple(self._load_order)

    def select(
        self,
        task: Task,
        *,
        max_skills: int | None = None,
        token_budget: int | None = None,
    ) -> SkillSelection:
        """Select at most two role-compatible skills for ``task``.

        Ranking is deterministic: number of matched triggers (descending), then skill
        name (ascending). A skill whose role list excludes the task's role is never a
        candidate. Skills that would exceed ``token_budget`` are skipped, not
        force-fitted, and an unmatched task selects nothing at all.
        """
        limit = self._max_skills if max_skills is None else max_skills
        if not 1 <= limit <= MAX_SKILLS_PER_TASK:
            raise ValueError(f"max_skills must be 1..{MAX_SKILLS_PER_TASK} (hard cap), got {limit}")
        budget = self._token_budget if token_budget is None else token_budget
        if budget < 1:
            raise ValueError(f"token_budget must be positive, got {budget}")

        haystack = _task_text(task)
        ranked: list[tuple[int, str, tuple[str, ...]]] = []
        for entry in self._scan():
            if task.role not in entry.meta.roles:
                continue
            matched = _matched_triggers(entry.meta.triggers, haystack)
            if matched:
                ranked.append((len(matched), entry.meta.name, matched))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        chosen: list[Skill] = []
        matched_pairs: list[tuple[str, tuple[str, ...]]] = []
        used = 0
        for _, name, matched in ranked:
            if len(chosen) >= limit:
                break
            skill = self.load(name)
            if used + skill.tokens > budget:
                continue
            chosen.append(skill)
            matched_pairs.append((name, matched))
            used += skill.tokens
        return SkillSelection(
            skills=tuple(chosen),
            matched=tuple(matched_pairs),
            tokens=used,
            budget=budget,
        )

    def inspect(self) -> SkillInspection:
        """Audit every skill without raising: the lint command's data source."""
        audits: list[SkillAudit] = []
        problems: list[SkillProblem] = []
        if not self._skills_dir.is_dir():
            problems.append(
                SkillProblem(
                    skill="",
                    code="missing_dir",
                    message=f"skills directory not found: {self._skills_dir}",
                )
            )
            return SkillInspection(problems=tuple(problems))

        seen: dict[str, Path] = {}
        for path in sorted(self._skills_dir.glob("*/SKILL.md")):
            directory = path.parent.name
            try:
                meta = _read_meta(path)
            except SkillError as exc:
                problems.append(SkillProblem(skill=directory, code=exc.code, message=exc.message))
                continue
            if meta.name != directory:
                problems.append(
                    SkillProblem(
                        skill=directory,
                        code="name_mismatch",
                        message=(
                            f"{path}: front-matter name '{meta.name}' does not match "
                            f"directory '{directory}'"
                        ),
                    )
                )
                continue
            if meta.name in seen:
                problems.append(
                    SkillProblem(
                        skill=meta.name,
                        code="duplicate",
                        message=f"{path}: duplicate skill name (also {seen[meta.name]})",
                    )
                )
                continue
            seen[meta.name] = path
            try:
                _, body = _split_document(path.read_text(encoding="utf-8"), path=path)
            except SkillError as exc:
                problems.append(SkillProblem(skill=meta.name, code=exc.code, message=exc.message))
                continue
            tokens = estimate_tokens(body)
            audits.append(
                SkillAudit(
                    name=meta.name,
                    tokens=tokens,
                    declared_max_tokens=meta.max_tokens,
                    path=path,
                )
            )
            if tokens > meta.max_tokens:
                problems.append(
                    SkillProblem(
                        skill=meta.name,
                        code="body_too_large",
                        message=(
                            f"body is {tokens} tokens; declared max_tokens is "
                            f"{meta.max_tokens} ({path})"
                        ),
                    )
                )
        audits.sort(key=lambda audit: audit.name)
        problems.sort(key=lambda problem: (problem.skill, problem.code, problem.message))
        return SkillInspection(audits=tuple(audits), problems=tuple(problems))

    def validate(self) -> tuple[SkillProblem, ...]:
        """Every problem found by :meth:`inspect`, as a flat tuple."""
        return self.inspect().problems


def lint_skills(
    skills_dir: Path | str | None = None,
    *,
    ceiling: int = STARTER_SKILL_TOKEN_CEILING,
    required: Sequence[str] = STARTER_SKILL_NAMES,
) -> tuple[SkillProblem, ...]:
    """Lint a skills directory: metadata, declared budgets, project ceiling, presence.

    Args:
        skills_dir: directory to lint; defaults to the shipped ``skills/``.
        ceiling: project-wide per-skill token ceiling (the brief's 400).
        required: skill names that must be installed (the seven starter skills).

    Returns:
        Sorted problems; an empty tuple means the directory is clean.
    """
    if ceiling < 1:
        raise ValueError(f"ceiling must be positive, got {ceiling}")
    loader = SkillLoader(skills_dir)
    inspection = loader.inspect()
    problems: list[SkillProblem] = list(inspection.problems)
    for audit in inspection.audits:
        if audit.tokens > ceiling:
            problems.append(
                SkillProblem(
                    skill=audit.name,
                    code="ceiling_exceeded",
                    message=(
                        f"body is {audit.tokens} tokens; project ceiling is {ceiling} "
                        f"({audit.path})"
                    ),
                )
            )
    present = {audit.name for audit in inspection.audits}
    for name in required:
        if name not in present:
            problems.append(
                SkillProblem(
                    skill=name,
                    code="missing",
                    message=f"required skill '{name}' is not installed in {loader.skills_dir}",
                )
            )
    problems.sort(key=lambda problem: (problem.skill, problem.code, problem.message))
    return tuple(problems)
