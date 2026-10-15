"""Lazy skill metadata indexing, deterministic selection and token-limit enforcement.

Only front matter is read by :meth:`SkillLoader.index`; skill bodies are read and
cached only after a task's role and trigger phrases select them. Agents do not depend
on this module: the ContextBuilder selects and injects skills in a later composition
step, while the small rendered index can be used in a stable cached prompt prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.contracts.enums import AgentRole
from app.contracts.task import Task
from app.providers.base import estimate_tokens

__all__ = [
    "Skill",
    "SkillFormatError",
    "SkillLoader",
    "SkillPolicy",
    "SkillTokenLimitError",
]

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONT_MATTER_KEYS = frozenset({"name", "description", "roles", "max_tokens", "triggers"})


class SkillFormatError(ValueError):
    """A skill file's location, front matter or metadata is invalid."""


class SkillTokenLimitError(SkillFormatError):
    """A loaded skill body exceeds its declared token limit."""


@dataclass(frozen=True, slots=True)
class SkillPolicy:
    """Hard caps applied by deterministic skill selection."""

    max_skills: int = 2
    max_tokens: int = 800

    def __post_init__(self) -> None:
        if not 0 <= self.max_skills <= 2:
            raise ValueError("max_skills must be between 0 and 2")
        if self.max_tokens < 0:
            raise ValueError("max_tokens cannot be negative")


@dataclass(frozen=True, slots=True)
class _SkillMetadata:
    name: str
    description: str
    roles: tuple[AgentRole, ...]
    max_tokens: int
    triggers: tuple[str, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class Skill:
    """A selected skill with the body loaded on demand."""

    name: str
    description: str
    roles: tuple[AgentRole, ...]
    max_tokens: int
    triggers: tuple[str, ...]
    body: str
    token_count: int


class SkillLoader:
    """Read skill metadata lazily and select no more than two bodies per task."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        policy: SkillPolicy | None = None,
    ) -> None:
        self.root = (
            Path(root) if root is not None else Path(__file__).resolve().parents[3] / "skills"
        )
        self.policy = policy if policy is not None else SkillPolicy()
        self._metadata: dict[str, _SkillMetadata] | None = None
        self._loaded: dict[str, Skill] = {}

    def index(self) -> list[dict[str, str]]:
        """Return only stable ``name``/``description`` pairs for the prompt prefix."""
        return [
            {"name": item.name, "description": item.description}
            for item in sorted(self._metadata_index().values(), key=lambda item: item.name)
        ]

    def render_index(self) -> str:
        """Render a deterministic, body-free skills index for a cached system prefix."""
        items = self.index()
        if not items:
            return "Available skills: none."
        return "Available skills:\n" + "\n".join(
            f"- {item['name']}: {item['description']}" for item in items
        )

    def select(
        self,
        task: Task,
        *,
        policy: SkillPolicy | None = None,
        token_budget: int | None = None,
    ) -> list[Skill]:
        """Select by exact case-insensitive trigger substrings, role, score and name.

        Candidates are ranked by the number of matched trigger phrases, then by skill
        name. Selection stops at the stricter of the policy token cap and the task's
        token budget. Bodies are loaded only for candidates that actually fit.
        """
        selected_policy = policy if policy is not None else self.policy
        if token_budget is not None and token_budget < 0:
            raise ValueError("token_budget cannot be negative")
        budget = min(
            selected_policy.max_tokens,
            task.token_budget if token_budget is None else token_budget,
        )
        if budget == 0 or selected_policy.max_skills == 0:
            return []

        text = _normalise_text(" ".join([task.title, task.instructions, *task.acceptance]))
        ranked: list[tuple[int, str, _SkillMetadata]] = []
        for metadata in self._metadata_index().values():
            if task.role not in metadata.roles:
                continue
            matches = sum(1 for trigger in metadata.triggers if _normalise_text(trigger) in text)
            if matches:
                ranked.append((-matches, metadata.name, metadata))
        ranked.sort(key=lambda candidate: (candidate[0], candidate[1]))

        result: list[Skill] = []
        used_tokens = 0
        for _, _, metadata in ranked:
            if len(result) >= selected_policy.max_skills:
                break
            skill = self.load(metadata.name)
            if used_tokens + skill.token_count > budget:
                continue
            result.append(skill)
            used_tokens += skill.token_count
        return result

    def load(self, name: str) -> Skill:
        """Load one body's content on demand and enforce its declared token ceiling."""
        metadata = self._metadata_index().get(name)
        if metadata is None:
            raise SkillFormatError(f"unknown skill {name!r}")
        cached = self._loaded.get(name)
        if cached is not None:
            return cached

        text = metadata.path.read_text(encoding="utf-8")
        body = _split_front_matter(text, metadata.path)[1].strip()
        if not body:
            raise SkillFormatError(f"{metadata.path}: skill body is empty")
        token_count = estimate_tokens(body)
        if token_count > metadata.max_tokens:
            raise SkillTokenLimitError(
                f"{metadata.path}: body uses {token_count} tokens, exceeding declared "
                f"max_tokens {metadata.max_tokens}"
            )
        skill = Skill(
            name=metadata.name,
            description=metadata.description,
            roles=metadata.roles,
            max_tokens=metadata.max_tokens,
            triggers=metadata.triggers,
            body=body,
            token_count=token_count,
        )
        self._loaded[name] = skill
        return skill

    def _metadata_index(self) -> dict[str, _SkillMetadata]:
        if self._metadata is not None:
            return self._metadata
        if not self.root.exists() or not self.root.is_dir():
            raise SkillFormatError(f"skills directory does not exist: {self.root}")
        metadata: dict[str, _SkillMetadata] = {}
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not directory.is_dir():
                continue
            if not _SKILL_NAME_RE.fullmatch(directory.name):
                raise SkillFormatError(f"invalid skill directory name: {directory.name!r}")
            path = directory / "SKILL.md"
            if not path.is_file():
                raise SkillFormatError(f"skill file is missing: {path}")
            front_matter, _ = _split_front_matter(_read_front_matter(path), path)
            item = _parse_metadata(front_matter, directory.name, path)
            metadata[item.name] = item
        self._metadata = metadata
        return metadata


def _read_front_matter(path: Path) -> str:
    """Read through the closing delimiter without reading the skill body."""
    with path.open(encoding="utf-8") as stream:
        lines = [stream.readline()]
        if lines[0].strip() != "---":
            raise SkillFormatError(f"{path}: front matter must start with ---")
        for line in stream:
            lines.append(line)
            if line.strip() == "---":
                return "".join(lines)
    raise SkillFormatError(f"{path}: front matter has no closing ---")


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML front matter and return it with the remaining body."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(f"{path}: front matter must start with ---")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                value = yaml.safe_load("".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise SkillFormatError(f"{path}: invalid YAML front matter: {exc}") from exc
            if not isinstance(value, dict):
                raise SkillFormatError(f"{path}: front matter must be a YAML mapping")
            return value, "".join(lines[index + 1 :])
    raise SkillFormatError(f"{path}: front matter has no closing ---")


def _parse_metadata(raw: dict[str, Any], directory_name: str, path: Path) -> _SkillMetadata:
    missing = _FRONT_MATTER_KEYS - set(raw)
    extra = set(raw) - _FRONT_MATTER_KEYS
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing keys {sorted(missing)}")
        if extra:
            details.append(f"unknown keys {sorted(extra)}")
        raise SkillFormatError(f"{path}: " + "; ".join(details))

    name = raw["name"]
    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        raise SkillFormatError(f"{path}: name must be a lowercase hyphenated slug")
    if name != directory_name:
        raise SkillFormatError(
            f"{path}: front matter name {name!r} does not match directory {directory_name!r}"
        )

    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        raise SkillFormatError(f"{path}: description must be a non-empty string")
    if len(description.split()) > 20:
        raise SkillFormatError(f"{path}: description exceeds the 20-word limit")

    raw_roles = raw["roles"]
    if not isinstance(raw_roles, list) or not raw_roles:
        raise SkillFormatError(f"{path}: roles must be a non-empty list")
    try:
        roles = tuple(AgentRole(role) for role in raw_roles)
    except (ValueError, TypeError) as exc:
        raise SkillFormatError(f"{path}: roles contain an unknown agent role") from exc
    if len(set(roles)) != len(roles):
        raise SkillFormatError(f"{path}: roles must not contain duplicates")

    max_tokens = raw["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise SkillFormatError(f"{path}: max_tokens must be a positive integer")

    raw_triggers = raw["triggers"]
    if not isinstance(raw_triggers, list) or not raw_triggers:
        raise SkillFormatError(f"{path}: triggers must be a non-empty list")
    if any(not isinstance(trigger, str) or not trigger.strip() for trigger in raw_triggers):
        raise SkillFormatError(f"{path}: triggers must contain non-empty strings")
    triggers = tuple(trigger.strip() for trigger in raw_triggers)
    if len({_normalise_text(trigger) for trigger in triggers}) != len(triggers):
        raise SkillFormatError(f"{path}: triggers must not contain duplicates")

    return _SkillMetadata(
        name=name,
        description=description.strip(),
        roles=roles,
        max_tokens=max_tokens,
        triggers=triggers,
        path=path,
    )


def _normalise_text(value: str) -> str:
    """Stable whitespace/case normalisation used for exact trigger matching."""
    return " ".join(value.casefold().split())
