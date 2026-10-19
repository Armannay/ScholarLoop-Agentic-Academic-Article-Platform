"""Cache-friendly prompt assembly: stable prefix, semi-stable context, volatile task.

Token-frugal by design (HARD RULE 3): the builder passes *references* (source-card
ids, section ids, a bounded digest slice) rather than whole histories, and the
result is always an :class:`~app.contracts.llm.LLMRequest` whose layout is fixed:

============================  ==========================================  ==========
Layer                         Contents                                    Cache
============================  ==========================================  ==========
STABLE PREFIX                 root rules -> project rules -> role rules   ``cache=True``
                              -> rubric excerpt -> skill index
                              (name + one line) -> brief
SEMI-STABLE                   selected skill bodies -> source cards the   ``cache=False``
                              task references
VOLATILE                      digest slice -> task -> text of the section ``cache=False``
                              being edited                                (user message)
============================  ==========================================  ==========

Why this order matters: providers cache a *prefix* of the prompt. Everything that
changes between iterations lives after everything that does not, so two calls for
the same role within a run share a byte-identical prefix and the vendor can serve
it from cache. ``test_governor_context.py`` asserts byte equality.

Rules layering
--------------
Project rules may **add** rules but never remove a rule marked mandatory. A
mandatory rule is a bullet under ``## Never`` in ``rules/root.rules.md`` (or any
line carrying ``[mandatory]``). If project rules supply their own ``## Never``
section, it must restate every mandatory rule or
:class:`MandatoryRuleViolation` is raised - a project cannot quietly drop
"never fabricate sources".

Trimming (only when the prompt exceeds the ceiling)
---------------------------------------------------
1. older digest detail,
2. optional skill bodies,
3. source-card notes (snippets shortened to their first sentence).

The task and the section being edited are **never** truncated. If the request is
still too large, :class:`ContextTooLarge` is raised so the orchestrator can split
the task or ask the user instead of silently losing the instructions.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, is_dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.contracts.enums import MessageRole
from app.contracts.llm import LLMMessage, LLMRequest, SystemBlock
from app.contracts.task import Task
from app.core.errors import ScholarLoopError
from app.core.policies import Policies
from app.providers.base import prompt_tokens

__all__ = [
    "ContextBuilder",
    "ContextTooLarge",
    "MandatoryRuleViolation",
    "RunState",
    "SkillInfo",
    "SkillRef",
    "build",
    "build_stable_prefix",
    "merge_rules",
    "validate_mandatory_rules",
]

#: Default prompt ceiling when neither the caller nor the policies supply one.
DEFAULT_MAX_INPUT_TOKENS = 24_000
#: Floor for a trimmed digest: below this the digest is dropped entirely.
MIN_DIGEST_TOKENS = 40


# --------------------------------------------------------------------------- errors
class ContextTooLarge(ScholarLoopError):
    """The prompt exceeds ``max_input_tokens`` even after every documented trim.

    The caller (orchestrator) must respond by splitting the task or asking the
    user. The task text and the section text are never silently truncated.
    """

    code = "context_too_large"
    http_status = 422


class MandatoryRuleViolation(ScholarLoopError):
    """Project rules attempted to remove a mandatory root rule."""

    code = "mandatory_rule_violation"
    http_status = 422


# --------------------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class SkillRef:
    """A skill as it appears in the stable index: name plus one line."""

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """A skill whose body may be included in the semi-stable layer."""

    name: str
    description: str = ""
    body: str = ""
    optional: bool = True


@dataclass
class RunState:
    """Everything the builder needs to know at call time.

    Only ``run_id`` has a default that matters; every other field is optional so
    the orchestrator can fill in what it has and tests can supply the minimum.

    A plain ``dict`` with these keys (or an object with these attributes) is also
    accepted by :func:`build`.
    """

    run_id: str = "default-run"
    iteration: int = 0
    role: str | None = None
    brief: Any | None = None
    digest: str | None = None
    section_text: str | dict[str, str] | None = None
    source_cards: list[Any] | None = None
    skills: list[Any] | None = None
    selected_skills: list[Any] | None = None
    policies: Policies | None = None
    rubric_excerpt: str | None = None
    root_rules: str | None = None
    project_rules: str | None = None
    role_rules: str | None = None
    role_rules_map: dict[str, str] | None = field(default=None)
    binding: Any | None = None
    max_input_tokens: int | None = None


# --------------------------------------------------------------------------- helpers
_MANDATORY_TAG = "[mandatory]"
_NEVER_RE = re.compile(r"^##\s+Never\b.*$", re.IGNORECASE | re.MULTILINE)
_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^(?:\d+\.|[-*])\s+")
_SEPARATORS = ("\n---\n", "\n## ", "\n\n", "\n")


def _field(state: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict or an object, defaulting quietly."""
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _as_text(value: Any) -> str:
    """Render a brief-ish object deterministically."""
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return "\n".join(f"{key}: {item}" for key, item in value.items())

    def _enum_attr(obj: Any, attr: str) -> str:
        raw = getattr(obj, attr, "")
        return str(getattr(raw, "value", raw))

    if all(hasattr(value, attr) for attr in ("topic", "article_type", "audience")):
        must_include = ", ".join(getattr(value, "must_include", []) or []) or "none"
        must_avoid = ", ".join(getattr(value, "must_avoid", []) or []) or "none"
        return (
            f"Topic: {value.topic}\n"
            f"Article type: {_enum_attr(value, 'article_type')}\n"
            f"Audience: {value.audience}\n"
            f"Language: {_enum_attr(value, 'language') if hasattr(value, 'language') else ''}\n"
            f"Target words: {getattr(value, 'target_words', '')}\n"
            f"Citation style: {_enum_attr(value, 'citation_style')}\n"
            f"Must include: {must_include}\n"
            f"Must avoid: {must_avoid}"
        )
    return str(value)


def _skill_name(skill: Any) -> str:
    if isinstance(skill, dict):
        return str(skill.get("name", "unnamed"))
    return str(getattr(skill, "name", "unnamed"))


def _skill_description(skill: Any) -> str:
    if isinstance(skill, dict):
        raw = skill.get("description", skill.get("one_line", ""))
    else:
        raw = getattr(skill, "description", getattr(skill, "one_line", ""))
    text = str(raw or "").strip().replace("\n", " ")
    return text or "(no description)"


def _skill_body(skill: Any) -> str:
    if isinstance(skill, dict):
        return str(skill.get("body", skill.get("text", "")) or "")
    return str(getattr(skill, "body", getattr(skill, "text", "")) or "")


def _skill_optional(skill: Any) -> bool:
    if isinstance(skill, dict):
        return bool(skill.get("optional", True))
    return bool(getattr(skill, "optional", True))


def _render_rubric(rubric: Any) -> str:
    """Render a rubric excerpt: a pre-rendered string, or a compact summary."""
    if rubric is None:
        return "(none)"
    if isinstance(rubric, str):
        return rubric.strip()
    gates = getattr(rubric, "gates", None)
    criteria = getattr(rubric, "criteria", None)
    if gates is None and criteria is None:
        return str(rubric)
    parts = ["Rubric excerpt:"]
    if gates:
        parts.append("Gates: " + ", ".join(str(getattr(g, "name", g)) for g in gates))
    if criteria:
        parts.append(
            "Criteria weights: "
            + ", ".join(f"{getattr(c, 'name', c)}={getattr(c, 'weight', '')}" for c in criteria)
        )
    return "\n".join(parts)


def _card_id(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("id", "?"))
    return str(getattr(card, "id", "?"))


def _card_title(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("title", "?"))
    return str(getattr(card, "title", "?"))


def _card_snippet(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("snippet", "") or "")
    return str(getattr(card, "snippet", "") or "")


def _card_authors(card: Any) -> str:
    raw = card.get("authors", []) if isinstance(card, dict) else getattr(card, "authors", [])
    return ", ".join(str(author) for author in raw or [])


def _card_year(card: Any) -> str:
    raw = card.get("year", "") if isinstance(card, dict) else getattr(card, "year", "")
    return str(raw or "")


def _render_cards(cards: list[Any], referenced: set[str], *, short: bool = False) -> str:
    """Render source cards, referenced ones first, ids as the citation handle."""
    ordered = sorted(cards, key=lambda card: (_card_id(card) not in referenced, _card_id(card)))
    lines = ["Source cards (cite by id):"]
    for card in ordered:
        snippet = _card_snippet(card)
        if short:
            snippet = snippet.split(". ")[0].rstrip()
            if snippet and not snippet.endswith("."):
                snippet += "."
        lines.append(
            f"- [{_card_id(card)}] {_card_title(card)}"
            f" ({_card_authors(card)}, {_card_year(card)}): {snippet}"
        )
    return "\n".join(lines)


def _estimate(text: str) -> int:
    """Chars/4 estimate for a block, matching the provider convention."""
    return 0 if not text else max(1, math.ceil(len(text) / 4))


# --------------------------------------------------------------------------- rules
def mandatory_rules(root_rules: str) -> list[str]:
    """Extract mandatory rules from root rules text.

    Rules are mandatory when they carry ``[mandatory]`` or when they are bullets
    under ``## Never`` (the shipped ``root.rules.md`` convention).
    """
    tagged: list[str] = []
    for line in root_rules.splitlines():
        if _MANDATORY_TAG in line:
            cleaned = line.replace(_MANDATORY_TAG, "").strip()
            cleaned = _BULLET_RE.sub("", cleaned).strip()
            if cleaned:
                tagged.append(cleaned)
    if tagged:
        return tagged

    match = _NEVER_RE.search(root_rules)
    if not match:
        return []
    following = _HEADING_RE.search(root_rules, match.end())
    block = root_rules[match.end() : following.start() if following else len(root_rules)]
    rules: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or not _BULLET_RE.match(stripped):
            continue
        cleaned = _BULLET_RE.sub("", stripped).strip()
        if cleaned:
            rules.append(cleaned)
    return rules


def _has_never_block(text: str) -> bool:
    return _NEVER_RE.search(text) is not None


def _rule_key(rule: str) -> str:
    """Normalised identity of a rule: its first three significant words.

    "Never fabricate sources, data, quotes" and "NEVER fabricate sources!" share
    the key ``never fabricate sources``: a project may rephrase or shorten a rule
    but cannot drop it.
    """
    words = re.findall(r"[a-z0-9]+", rule.lower())
    return " ".join(words[:3])


def validate_mandatory_rules(root_rules: str, project_rules: str | None) -> None:
    """Reject project rules that would remove a mandatory rule.

    Additive project rules (no ``## Never`` heading) are always allowed: the
    stable prefix keeps the root rules verbatim, so nothing can be lost. A
    project-level ``## Never`` block replaces that section of the prompt, so it
    must restate every mandatory rule (matching on the rule's leading words).

    Raises:
        MandatoryRuleViolation: naming the rules that would disappear.
    """
    if not project_rules or not _has_never_block(project_rules):
        return
    required = mandatory_rules(root_rules)
    if not required:
        return
    offered = " ".join(re.findall(r"[a-z0-9]+", project_rules.lower()))
    missing = [rule for rule in required if _rule_key(rule) not in offered]
    if missing:
        raise MandatoryRuleViolation(
            "project rules remove mandatory rule(s): " + "; ".join(missing[:3]),
            missing=missing,
        )


def merge_rules(
    root_rules: str,
    project_rules: str | None = None,
    role_rules: str | None = None,
) -> str:
    """Compose root -> project -> role rules with mandatory protection.

    Root rules always come first and are never dropped. Project rules follow;
    role rules follow those. A project ``## Never`` block must restate every
    mandatory root rule (see :func:`validate_mandatory_rules`).
    """
    root = (root_rules or "").strip()
    validate_mandatory_rules(root, project_rules)

    parts = [part for part in (root, (project_rules or "").strip()) if part]
    merged = "\n\n".join(parts)
    if role_rules and role_rules.strip():
        merged = (merged + "\n\n" + role_rules.strip()).strip() if merged else role_rules.strip()
    return merged


# --------------------------------------------------------------------------- prefix
def build_stable_prefix(
    *,
    role: str,
    run_id: str = "default-run",
    root_rules: str = "",
    project_rules: str | None = None,
    role_rules: str | None = None,
    rubric_excerpt: Any = None,
    skill_index: list[Any] | None = None,
    brief: Any = None,
) -> str:
    """Compose the cacheable stable prefix. Deterministic and iteration-free.

    The same ``(role, inputs)`` always produces a byte-identical string: no
    timestamps, no iteration counters, skills sorted by name, fixed separators.
    ``run_id`` is accepted so callers can key their own prefix cache by run; it is
    deliberately not embedded, which lets identical briefs reuse a provider cache
    across runs.
    """
    rules = merge_rules(root_rules, project_rules, role_rules) or "(none)"

    if skill_index:
        lines = ["Skills index (name: one line):"]
        for skill in sorted(skill_index, key=_skill_name):
            lines.append(f"- {_skill_name(skill)}: {_skill_description(skill)}")
        skills = "\n".join(lines)
    else:
        skills = "Skills index (name: one line):\n(none)"

    blocks = [
        f"Role: {role}",
        "Rules:\n" + rules,
        "Rubric:\n" + _render_rubric(rubric_excerpt),
        skills,
        "Brief:\n" + _as_text(brief),
    ]
    return "\n\n---\n\n".join(block.strip() for block in blocks)


# --------------------------------------------------------------------------- trims
def _truncate_digest(digest: str, keep_tokens: int) -> str:
    """Keep the most recent ``keep_tokens`` of a digest.

    Digests are written oldest-first. We split on the strongest separator present
    and keep whole trailing entries where possible; a single oversized entry is
    cut from the front so the newest text survives.
    """
    if _estimate(digest) <= keep_tokens:
        return digest
    if keep_tokens <= 0:
        return ""
    for separator in _SEPARATORS:
        if separator not in digest:
            continue
        entries = digest.split(separator)
        kept: list[str] = []
        for entry in reversed(entries):
            candidate = separator.join([entry, *kept])
            if _estimate(candidate) <= keep_tokens:
                kept.insert(0, entry)
            else:
                break
        if kept:
            return separator.join(kept)
        # Even the newest entry is too large: keep its tail.
        return entries[-1][-keep_tokens * 4 :]
    return digest[-keep_tokens * 4 :]


def _render_skills(skills: list[Any], *, include_optional: bool) -> str:
    blocks: list[str] = []
    for skill in skills:
        if not include_optional and _skill_optional(skill):
            continue
        body = _skill_body(skill).strip()
        if not body:
            continue
        blocks.append(f"Skill {_skill_name(skill)}:\n{body}")
    return "\n\n".join(blocks)


def _prompt_tokens(stable: str, semi: str, volatile: str) -> int:
    """Exact token count of the request shape the builder emits."""
    system = [SystemBlock(text=stable, cache=True)]
    if semi:
        system.append(SystemBlock(text=semi))
    request = LLMRequest(
        system_blocks=system,
        messages=[LLMMessage(role=MessageRole.USER, text=volatile or " ")],
        max_output_tokens=1,
    )
    return prompt_tokens(request)


@lru_cache(maxsize=8)
def _read_rules_file(*parts: str) -> str:
    """Read a rules file from the repository, or return "" when absent.

    Cached: prompt assembly happens on every call, file I/O should not. The
    rules directory sits next to ``backend/`` in the repository layout; tests and
    deployments normally pass the text explicitly, this is a convenience for
    first-run scripts and for the orchestrator's defaults.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent.joinpath(*parts)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


def default_root_rules() -> str:
    """The shipped ``rules/root.rules.md`` (empty when the file is absent)."""
    return _read_rules_file("rules", "root.rules.md")


def default_role_rules(role: str) -> str:
    """The shipped ``rules/agents/<role>.rules.md`` (empty when absent)."""
    return _read_rules_file("rules", "agents", f"{role}.rules.md")


def _ceiling_for(
    run_state: Any,
    binding: Any | None,
    policies: Policies | None,
    explicit: int | None,
) -> int:
    if explicit is not None:
        return explicit
    candidates = (
        _field(run_state, "max_input_tokens"),
        getattr(binding, "max_input_tokens", None),
        getattr(_field(run_state, "binding"), "max_input_tokens", None),
        getattr(policies, "call_input_cap", None),
        getattr(_field(run_state, "policies"), "call_input_cap", None),
    )
    for candidate in candidates:
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return DEFAULT_MAX_INPUT_TOKENS


# --------------------------------------------------------------------------- build
def build(
    task: Task,
    run_state: Any,
    *,
    binding: Any | None = None,
    policies: Policies | None = None,
    max_input_tokens: int | None = None,
) -> LLMRequest:
    """Build the :class:`LLMRequest` for one task execution.

    Args:
        task: the task being executed. Its ``instructions``, ``acceptance`` and
            ``token_budget`` are placed in the volatile layer, verbatim.
        run_state: a :class:`RunState`, an object with the same attributes, or a
            dict with the same keys.
        binding: optional :class:`~app.contracts.binding.AgentBinding`, supplying
            the prompt ceiling and the output cap.
        policies: optional effective :class:`Policies` (overrides those in state).
        max_input_tokens: explicit prompt ceiling; wins over everything else.

    Returns:
        An :class:`LLMRequest` with ``system_blocks[0]`` the cacheable stable
        prefix and the volatile task in the final user message.

    Raises:
        MandatoryRuleViolation: project rules drop a mandatory root rule.
        ContextTooLarge: still above the ceiling after the documented trims.
    """
    role = str(_field(run_state, "role") or getattr(task.role, "value", task.role))
    run_id = str(_field(run_state, "run_id") or "default-run")

    role_rules = _field(run_state, "role_rules")
    if role_rules is None:
        role_map = _field(run_state, "role_rules_map") or {}
        role_rules = role_map.get(role) if isinstance(role_map, dict) else None
    if role_rules is None:
        role_rules = default_role_rules(role) or None

    root_rules = _field(run_state, "root_rules")
    if root_rules is None:
        root_rules = default_root_rules()

    skill_index = _field(run_state, "skills")
    if skill_index is None:
        skill_index = _field(run_state, "skill_index")

    stable = build_stable_prefix(
        role=role,
        run_id=run_id,
        root_rules=str(root_rules or ""),
        project_rules=_field(run_state, "project_rules"),
        role_rules=role_rules,
        rubric_excerpt=_field(run_state, "rubric_excerpt"),
        skill_index=skill_index,
        brief=_field(run_state, "brief"),
    )

    ceiling = _ceiling_for(
        run_state,
        binding,
        policies if policies is not None else _field(run_state, "policies"),
        max_input_tokens,
    )

    selected_skills = list(
        _field(run_state, "selected_skills") or _field(run_state, "skill_bodies") or []
    )
    referenced = {str(source_id) for source_id in (task.source_ids or [])}
    # The layout names "source cards the task references": a writer gets the
    # cards its task was planned around, not the whole source store. Sending
    # every card would grow linearly with the bibliography on every call.
    source_cards = [
        card
        for card in (_field(run_state, "source_cards") or _field(run_state, "cards") or [])
        if _card_id(card) in referenced
    ]

    digest_raw = _field(run_state, "digest")
    if digest_raw is None:
        for alias in ("digest_slice", "digest_full", "claude_md"):
            digest_raw = _field(run_state, alias)
            if digest_raw is not None:
                break
    digest = str(digest_raw or "").strip()

    def render_volatile(digest_text: str) -> str:
        parts: list[str] = []
        if digest_text:
            parts.append("Digest (bounded; oldest detail may be trimmed):\n" + digest_text)
        acceptance = "\n".join(f"- {item}" for item in task.acceptance)
        parts.append(
            f"Task: {task.title}\n"
            f"Role: {getattr(task.role, 'value', task.role)}\n"
            f"Instructions: {task.instructions}\n"
            f"Acceptance:\n{acceptance}\n"
            f"Token budget: {task.token_budget}"
        )
        if task.section_ids:
            parts.append("Sections in scope: " + ", ".join(task.section_ids))
        section_text = _field(run_state, "section_text")
        if section_text is None:
            section_text = _field(run_state, "section_texts")
        if isinstance(section_text, dict):
            wanted = task.section_ids or list(section_text)
            rendered = [
                f"Section {sid} (current text):\n{section_text[sid]}"
                for sid in wanted
                if sid in section_text
            ]
            if rendered:
                parts.append("\n\n".join(rendered))
        elif section_text:
            parts.append("Current text of the section being edited:\n" + str(section_text).strip())
        return "\n\n---\n\n".join(parts)

    # -- pre-trim assembly --------------------------------------------------
    semi = _render_skills(selected_skills, include_optional=True)
    if source_cards:
        cards_text = _render_cards(source_cards, referenced)
        semi = (semi + "\n\n" + cards_text).strip() if semi else cards_text
    volatile = render_volatile(digest)
    total = _prompt_tokens(stable, semi, volatile)

    # -- trim 1: older digest detail ---------------------------------------
    if total > ceiling and digest:
        excess = total - ceiling
        keep = max(0, _estimate(digest) - excess - MIN_DIGEST_TOKENS)
        digest = _truncate_digest(digest, keep)
        volatile = render_volatile(digest)
        total = _prompt_tokens(stable, semi, volatile)
        if total > ceiling:
            # Sacrifice the rest of the digest before touching skills: the digest
            # is a convenience, the skills are instructions.
            digest = ""
            volatile = render_volatile(digest)
            total = _prompt_tokens(stable, semi, volatile)

    # -- trim 2: optional skill bodies -------------------------------------
    if total > ceiling and any(_skill_optional(s) and _skill_body(s) for s in selected_skills):
        mandatory_skills = [s for s in selected_skills if not _skill_optional(s)]
        skills_text = _render_skills(mandatory_skills, include_optional=False)
        cards_text = _render_cards(source_cards, referenced) if source_cards else ""
        semi = "\n\n".join(part for part in (skills_text, cards_text) if part)
        total = _prompt_tokens(stable, semi, volatile)

    # -- trim 3: source-card notes -----------------------------------------
    if total > ceiling and source_cards:
        skills_text = _render_skills(selected_skills, include_optional=False)
        short_cards = _render_cards(source_cards, referenced, short=True)
        semi = "\n\n".join(part for part in (skills_text, short_cards) if part)
        total = _prompt_tokens(stable, semi, volatile)

    if total > ceiling:
        raise ContextTooLarge(
            f"prompt is {total} tokens after all trims (ceiling {ceiling}); "
            f"stable={_estimate(stable)} semi={_estimate(semi)} volatile={_estimate(volatile)}",
            ceiling=ceiling,
            tokens=total,
            stable_tokens=_estimate(stable),
            volatile_tokens=_estimate(volatile),
        )

    # -- output cap ---------------------------------------------------------
    output_cap = getattr(binding, "max_output_tokens", None) or getattr(
        _field(run_state, "binding"), "max_output_tokens", None
    )
    if not isinstance(output_cap, int) or output_cap <= 0:
        output_cap = min(4_000, max(256, ceiling // 4))
    # A task's token_budget is a ceiling the agent is graded against; asking the
    # provider for more output than that guarantees an overrun the precheck
    # cannot see, because output tokens are only real after the call.
    output_cap = min(output_cap, task.token_budget)

    system_blocks = [SystemBlock(text=stable, cache=True)]
    if semi:
        system_blocks.append(SystemBlock(text=semi))

    return LLMRequest(
        system_blocks=system_blocks,
        messages=[LLMMessage(role=MessageRole.USER, text=volatile)],
        max_output_tokens=max(1, min(64_000, output_cap)),
    )


class ContextBuilder:
    """Stateful wrapper that holds the rule texts for many builds.

    The orchestrator constructs one builder per run; it loads rules once, injects
    them into every state, and caches the stable prefix per ``(run_id, role)`` so
    drift is detected immediately rather than at the provider.

    Example::

        builder = ContextBuilder(
            root_rules=Path("rules/root.rules.md").read_text(),
            rubric_excerpt=rubric,
            skill_index=[SkillRef("search", "Metadata lookup over Crossref")],
        )
        request = builder.build(task, RunState(run_id=run.id, brief=brief))
    """

    def __init__(
        self,
        *,
        root_rules: str = "",
        rubric_excerpt: Any = None,
        skill_index: list[Any] | None = None,
        policies: Policies | None = None,
    ) -> None:
        self._root_rules = root_rules
        self._rubric_excerpt = rubric_excerpt
        self._skill_index = list(skill_index) if skill_index else None
        self._policies = policies
        self._prefix_cache: dict[tuple[str, str], str] = {}

    def _augment(self, run_state: Any) -> Any:
        if isinstance(run_state, dict):
            enriched = dict(run_state)
            enriched.setdefault("root_rules", self._root_rules)
            if self._rubric_excerpt is not None:
                enriched.setdefault("rubric_excerpt", self._rubric_excerpt)
            if self._skill_index is not None:
                enriched.setdefault("skills", self._skill_index)
            if self._policies is not None:
                enriched.setdefault("policies", self._policies)
            return enriched

        defaults = {
            "root_rules": self._root_rules,
            "rubric_excerpt": self._rubric_excerpt,
            "skills": self._skill_index,
            "policies": self._policies,
        }

        # A dataclass RunState is copied, never mutated: the same state is
        # reused across tasks and builders, and silently writing one builder's
        # rules into it would make the next builder's defaults unreachable.
        if is_dataclass(run_state) and not isinstance(run_state, type):
            changes = {
                attr: value
                for attr, value in defaults.items()
                if value is not None
                and hasattr(run_state, attr)
                and not getattr(run_state, attr, None)
            }
            return replace(run_state, **changes) if changes else run_state

        for attr, value in defaults.items():
            if value is not None and not getattr(run_state, attr, None):
                try:
                    setattr(run_state, attr, value)
                except (AttributeError, ValueError):  # frozen object: default not applied
                    continue
        return run_state

    def build(self, task: Task, run_state: Any, **kwargs: Any) -> LLMRequest:
        """Build a request, injecting the builder's defaults into ``run_state``."""
        state = self._augment(run_state)
        request = build(task, state, **kwargs)
        role = str(_field(state, "role") or getattr(task.role, "value", task.role))
        run_id = str(_field(state, "run_id") or "default-run")
        self._prefix_cache[(run_id, role)] = request.system_blocks[0].text
        return request

    def stable_prefix(self, task: Task, run_state: Any) -> str:
        """The stable prefix for this task/state (cached per ``(run_id, role)``)."""
        role = str(_field(run_state, "role") or getattr(task.role, "value", task.role))
        run_id = str(_field(run_state, "run_id") or "default-run")
        cached = self._prefix_cache.get((run_id, role))
        if cached is not None:
            return cached
        state = self._augment(run_state)
        request = build(task, state, max_input_tokens=10**9)
        return request.system_blocks[0].text
