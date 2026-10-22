"""Cache-friendly prompt assembly with a fixed layout and a documented trim order.

The prompt is three tiers, always in this order:

.. code-block:: text

    STABLE  (cache=true)   root rules -> project rules -> role rules -> rubric excerpt
                           -> skill index (name + one line) -> brief
    SEMI    (cache=false)  selected skill bodies -> the source cards this task names
    VOLATILE               digest slice -> task -> text of the section(s) being edited

Two properties follow from the layout and are enforced here, not hoped for:

* **The stable prefix is byte-identical for every call of the same role within a run.**
  Nothing volatile (iteration number, task, section text, digest) may enter it; the
  prefix hash is returned so a test can prove identity across different tasks.
* **Rules are never dropped.** Project rules are appended after the mandatory root
  rules and :class:`MandatoryRuleGuard` rejects a project-rule document that tries to
  remove or override a root rule, or a composed prefix that lost one.

When the prompt does not fit ``max_input_tokens`` the builder trims in this order and
records every step in :attr:`ContextPlan.trims`:

1. **older digest detail** - keep the newest tail of ``claude.md``;
2. **optional skill bodies** - drop them (the skill *index* stays; it is part of the
   stable prefix);
3. **source-card notes** - drop snippets, keep ids and metadata so citation stays
   possible;
4. **raise :class:`ContextTooLarge`.**

The task and the section text being edited are *never* truncated: silently shortening
the instruction the model must obey, or the text it must patch, produces confidently
wrong output. Better to refuse the call and let the loop controller decide.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from app.agents.context import TASK_INSTRUCTION, load_role_rules
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, MessageRole
from app.contracts.llm import LLMMessage, LLMRequest, SystemBlock
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError, ScholarLoopError
from app.core.hashing import content_hash
from app.core.policies import DEFAULT_POLICIES
from app.core.rubric import Rubric, resolve_sections
from app.providers.base import CHARS_PER_TOKEN, estimate_tokens
from app.skills.loader import SkillLoader, SkillSelection

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DIGEST_TRIM_MARKER",
    "TASK_INSTRUCTION",
    "TRIM_ORDER",
    "ContextBuilder",
    "ContextPlan",
    "ContextTooLarge",
    "GovernorRunState",
    "MandatoryRuleGuard",
    "MandatoryRuleRemoved",
    "RuleBundle",
    "RuleLibrary",
    "TrimAction",
    "TrimStep",
    "mandatory_rules",
]

#: Default completion ceiling when the caller does not name one. Callers with a binding
#: should pass ``binding.max_output_tokens`` instead.
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 4_096
#: Prefix of a digest that had to lose its oldest detail.
DIGEST_TRIM_MARKER: Final[str] = "[digest trimmed: newest detail retained]\n"

_MANDATORY_ITEM_RE = re.compile(r"^\s*(?:\d+\.|[-*])\s+(\S.*)$")
_REMOVAL_DIRECTIVE_RE = re.compile(
    r"^\s*(?:\d+\.|[-*])\s*(?:remove|delete|disable|override|ignore)\s*[:\-]",
    re.IGNORECASE,
)


class ContextTooLarge(ScholarLoopError):
    """The prompt cannot fit ``max_input_tokens`` without cutting the task or section."""

    code = "context_too_large"
    http_status = 413


class MandatoryRuleRemoved(ScholarLoopError):
    """A rule marked mandatory in the root rules is missing or was overridden."""

    code = "mandatory_rule_removed"
    http_status = 500


class TrimStep(StrEnum):
    """The trim order, outermost (cheapest to lose) first."""

    DIGEST_DETAIL = "digest_detail"
    OPTIONAL_SKILLS = "optional_skills"
    SOURCE_CARD_NOTES = "source_card_notes"


#: Documented trim order. Anything outside this list is a bug, not a strategy.
TRIM_ORDER: Final[tuple[TrimStep, ...]] = (
    TrimStep.DIGEST_DETAIL,
    TrimStep.OPTIONAL_SKILLS,
    TrimStep.SOURCE_CARD_NOTES,
)


def _normalise(text: str) -> str:
    """Collapse whitespace so a rule compares equal across line wrapping."""
    return " ".join(text.split())


def mandatory_rules(text: str) -> tuple[str, ...]:
    """Numbered and bulleted rule items in a rule document, in file order.

    The root rules file marks every item in its ``Never`` / ``Always`` lists this way;
    those are the items a project may add to but never remove.
    """
    rules: list[str] = []
    for line in text.splitlines():
        match = _MANDATORY_ITEM_RE.match(line)
        if match:
            rules.append(match.group(1).strip())
    return tuple(rules)


class MandatoryRuleGuard:
    """Rejects any attempt to weaken the root rules.

    Two deterministic checks:

    1. no project-rule line may be a removal directive
       (``- remove: ...``, ``1. override: ...``);
    2. every mandatory root rule must still appear in the composed prefix.

    The guard cannot judge editorial intent - it enforces the two mechanical failures
    that actually happen: a project file that re-writes the rules, and a composition bug
    that drops the root document.
    """

    def __init__(self, mandatory: Sequence[str]) -> None:
        self._mandatory = tuple(mandatory)

    @classmethod
    def from_root(cls, root_text: str) -> MandatoryRuleGuard:
        """Build the guard from a root rules document."""
        return cls(mandatory_rules(root_text))

    @property
    def mandatory(self) -> tuple[str, ...]:
        """The protected rule items."""
        return self._mandatory

    def check(self, *, project_text: str, composed_text: str) -> None:
        """Raise :class:`MandatoryRuleRemoved` when a rule was removed or overridden.

        Args:
            project_text: the project-specific additions, if any.
            composed_text: the full prefix text that will reach the model.
        """
        for line in project_text.splitlines():
            if _REMOVAL_DIRECTIVE_RE.match(line):
                raise MandatoryRuleRemoved(
                    "project rules may add to the root rules but never remove them",
                    directive=line.strip()[:120],
                )
        haystack = _normalise(composed_text)
        missing = [rule for rule in self._mandatory if _normalise(rule) not in haystack]
        if missing:
            raise MandatoryRuleRemoved(
                "the composed prompt lost a mandatory root rule",
                missing=[rule[:120] for rule in missing[:3]],
                missing_count=len(missing),
            )


@dataclass(frozen=True)
class RuleBundle:
    """The three rule documents that form the head of the stable prefix."""

    root: str
    project: str = ""
    role: str = ""

    def __post_init__(self) -> None:
        if not self.root.strip():
            raise ValueError("root rules must not be empty; an agent without hard rules is invalid")

    @property
    def mandatory(self) -> tuple[str, ...]:
        """Rule items the root document marks mandatory."""
        return mandatory_rules(self.root)

    @property
    def guard(self) -> MandatoryRuleGuard:
        """A guard bound to this bundle's mandatory rules."""
        return MandatoryRuleGuard(self.mandatory)

    def blocks(self) -> tuple[tuple[str, str], ...]:
        """Non-empty rule documents in prompt order."""
        return tuple(
            (label, text.strip())
            for label, text in (("root", self.root), ("project", self.project), ("role", self.role))
            if text.strip()
        )

    def compose(self) -> str:
        """All rule documents joined exactly as they appear in the prefix."""
        return "\n\n".join(text for _, text in self.blocks())


class RuleLibrary:
    """Reads root rules, role rules and optional project rules from disk.

    Args:
        rules_dir: directory holding ``root.rules.md`` and ``agents/<role>.rules.md``.
        project_rules: inline project additions (highest precedence is not a thing here:
            they are appended, never merged over the root).
        project_rules_path: a file with project additions, read once at construction.
    """

    def __init__(
        self,
        rules_dir: Path | str,
        *,
        project_rules: str = "",
        project_rules_path: Path | str | None = None,
    ) -> None:
        self._rules_dir = Path(rules_dir)
        project_text = project_rules
        if project_rules_path is not None:
            path = Path(project_rules_path)
            if not path.is_file():
                raise ConfigError(f"project rules file not found: {path}")
            project_text = path.read_text(encoding="utf-8")
        self._project = project_text.strip()
        self._cache: dict[AgentRole, RuleBundle] = {}

    @property
    def rules_dir(self) -> Path:
        return self._rules_dir

    def load(self, role: AgentRole) -> RuleBundle:
        """Rule bundle for ``role``; read once and cached.

        Raises:
            ConfigError: a rules file is missing - a deployment error, not a reason to
                run an agent without its hard rules.
        """
        cached = self._cache.get(role)
        if cached is not None:
            return cached
        root, role_text = load_role_rules(self._rules_dir, role)
        bundle = RuleBundle(root=root, project=self._project, role=role_text)
        self._cache[role] = bundle
        return bundle


@runtime_checkable
class GovernorRunState(Protocol):
    """What the governor reads from the orchestrator's run state.

    The four required properties match :class:`app.agents.context.RunState`, so the
    orchestrator can pass its own object unchanged. Two optional extras are read
    defensively with ``getattr`` because they belong to later steps:

    * ``digest: str`` - the current ``claude.md`` slice;
    * ``section_text(section_id) -> str`` or ``section_texts: Mapping[str, str]`` - the
      body of a section. A builder that cannot find one renders an explicit "no text
      yet" marker instead of inventing prose.
    """

    @property
    def iter(self) -> int:
        """Current loop iteration."""
        ...

    @property
    def brief(self) -> Brief | None:
        """The user's brief, when intake has produced one."""
        ...

    @property
    def sections(self) -> Sequence[SectionState]:
        """Every section's state, in article order."""
        ...

    @property
    def sources(self) -> Sequence[SourceCard]:
        """SourceCards gathered so far."""
        ...


@dataclass(frozen=True)
class TrimAction:
    """One trim the builder applied, so the decision is auditable."""

    step: TrimStep
    detail: str
    tokens_freed: int

    def __post_init__(self) -> None:
        if self.tokens_freed < 0:
            raise ValueError("tokens_freed must be >= 0")


@dataclass(frozen=True)
class ContextPlan:
    """The assembled prompt plus the numbers needed to reason about it."""

    request: LLMRequest
    stable_blocks: tuple[SystemBlock, ...]
    semi_stable_blocks: tuple[SystemBlock, ...]
    volatile_text: str
    prefix_hash: str
    stable_tokens: int
    semi_stable_tokens: int
    volatile_tokens: int
    input_tokens: int
    max_input_tokens: int
    schema_tokens: int = 0
    trims: tuple[TrimAction, ...] = ()

    @property
    def trimmed(self) -> bool:
        """True when anything had to be dropped to fit."""
        return bool(self.trims)

    @property
    def stable_prefix(self) -> str:
        """Exact text of the cacheable prefix."""
        return "\n\n".join(block.text for block in self.stable_blocks)

    @property
    def skills(self) -> tuple[str, ...]:
        """Skill names whose bodies survived trimming."""
        names: list[str] = []
        for block in self.semi_stable_blocks:
            for line in block.text.splitlines():
                if line.startswith("## Skill: "):
                    names.append(line.removeprefix("## Skill: ").strip())
        return tuple(names)


class ContextBuilder:
    """Builds one cache-friendly :class:`~app.contracts.llm.LLMRequest` per task.

    Args:
        rules: rule library (per-role loading) or one fixed bundle for all roles.
        rubric: the loaded rubric; only an excerpt for the brief's article type is sent.
        skills: the central skill loader - the only way skill text enters a prompt.
        max_input_tokens: prompt ceiling; defaults to ``Policies.call_input_cap``.
        max_output_tokens: completion ceiling; callers with a binding pass its value.
    """

    def __init__(
        self,
        *,
        rules: RuleLibrary | RuleBundle,
        rubric: Rubric,
        skills: SkillLoader,
        max_input_tokens: int = DEFAULT_POLICIES.call_input_cap,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_input_tokens < 1:
            raise ValueError(f"max_input_tokens must be >= 1, got {max_input_tokens}")
        if max_output_tokens < 1:
            raise ValueError(f"max_output_tokens must be >= 1, got {max_output_tokens}")
        self._rules = rules
        self._rubric = rubric
        self._skills = skills
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens

    # -- public ------------------------------------------------------------
    def build(
        self,
        task: Task,
        run_state: GovernorRunState,
        *,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMRequest:
        """Return the request for ``task``; see :meth:`plan` for the accounting."""
        return self.plan(
            task,
            run_state,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            json_schema=json_schema,
        ).request

    def plan(
        self,
        task: Task,
        run_state: GovernorRunState,
        *,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> ContextPlan:
        """Assemble the prompt, applying the documented trim order when needed.

        Raises:
            ContextTooLarge: the task and section text alone do not fit after every
                allowed trim. Nothing the model must obey or patch was cut.
            MandatoryRuleRemoved: project rules tried to remove a root rule.
        """
        limit = self._max_input_tokens if max_input_tokens is None else max_input_tokens
        output_tokens = self._max_output_tokens if max_output_tokens is None else max_output_tokens
        if limit < 1 or output_tokens < 1:
            raise ValueError("token ceilings must be >= 1")
        # A structured call sends its schema on every request: it is prompt tokens, and
        # leaving it out of the ceiling would under-count exactly the expensive calls.
        schema_tokens = (
            0
            if json_schema is None
            else estimate_tokens(json.dumps(json_schema, sort_keys=True, separators=(",", ":")))
        )

        bundle = self._bundle(task)
        guard = bundle.guard
        guard.check(project_text=bundle.project, composed_text=bundle.compose())

        stable_blocks = self._stable_blocks(bundle, run_state.brief)
        stable_tokens = _tokens(stable_blocks)

        selection = self._skills.select(task)
        skill_blocks = self._skill_blocks(selection)
        cards = self._task_cards(task, run_state)
        card_blocks = self._card_blocks(cards, with_notes=True)

        digest_full = _digest_of(run_state)
        task_text = self._task_text(task)
        section_text = self._section_text(task, run_state)
        fixed_tokens = (
            stable_tokens
            + estimate_tokens(TASK_INSTRUCTION)
            + estimate_tokens(task_text)
            + estimate_tokens(section_text)
            + schema_tokens
        )
        full_digest_tokens = estimate_tokens(digest_full)
        trims: list[TrimAction] = []

        # Trim loop. Each round re-derives the largest digest that fits next to the
        # core, so dropping something expensive never silently throws away digest
        # detail that would now fit. Every round either succeeds or removes one thing,
        # so this terminates; the exact check uses the real volatile text, not a sum of
        # separately rounded estimates.
        while True:
            core_tokens = fixed_tokens + _tokens(skill_blocks) + _tokens(card_blocks)
            budget = limit - core_tokens
            digest = _trim_digest_to(digest_full, budget)
            # The volatile message joins its parts, so separately rounded estimates can
            # be a token or two optimistic. Correct against the *real* message: shrink
            # the digest until the actual text fits, not until a sum of estimates does.
            for _ in range(8):
                volatile_text = self._volatile_text(digest, task_text, section_text)
                input_tokens = (
                    stable_tokens
                    + _tokens(skill_blocks)
                    + _tokens(card_blocks)
                    + schema_tokens
                    + estimate_tokens(volatile_text)
                )
                overflow = input_tokens - limit
                if overflow <= 0 or budget <= 0 or not digest:
                    break
                tighter = _trim_digest_to(digest_full, estimate_tokens(digest) - overflow)
                if tighter == digest:
                    break
                digest = tighter
            if input_tokens <= limit:
                break

            # 1. older digest detail is already trimmed to its maximum above. If the
            #    loop is still here, the digest is empty: move to the next tier.
            # 2. optional skill bodies ------------------------------------------------
            if skill_blocks:
                dropped = skill_blocks.pop()
                trims.append(
                    TrimAction(
                        step=TrimStep.OPTIONAL_SKILLS,
                        detail=f"dropped the skill body '{_skill_name(dropped.text)}'; "
                        "the skill index stays",
                        tokens_freed=estimate_tokens(dropped.text),
                    )
                )
                continue

            # 3. source-card notes ---------------------------------------------------
            without_notes = self._card_blocks(cards, with_notes=False)
            freed = _tokens(card_blocks) - _tokens(without_notes)
            if freed > 0:
                card_blocks = without_notes
                trims.append(
                    TrimAction(
                        step=TrimStep.SOURCE_CARD_NOTES,
                        detail="dropped source-card snippets; ids and metadata kept",
                        tokens_freed=freed,
                    )
                )
                continue

            # 4. refuse rather than cut the task or the section being edited ---------
            raise ContextTooLarge(
                f"prompt needs {input_tokens} input tokens; max_input_tokens is {limit}. "
                "The task and the section text are never truncated, so this call is "
                "refused instead.",
                stable_tokens=stable_tokens,
                semi_stable_tokens=_tokens(skill_blocks) + _tokens(card_blocks),
                volatile_tokens=estimate_tokens(volatile_text),
                schema_tokens=schema_tokens,
                max_input_tokens=limit,
                role=task.role.value,
                task_id=task.id,
                trims=[action.step.value for action in trims],
            )

        if estimate_tokens(digest) < full_digest_tokens:
            trims.append(
                TrimAction(
                    step=TrimStep.DIGEST_DETAIL,
                    detail="kept the newest digest detail only",
                    tokens_freed=full_digest_tokens - estimate_tokens(digest),
                )
            )
        trims.sort(key=lambda action: TRIM_ORDER.index(action.step))

        semi_tokens = _tokens(skill_blocks) + _tokens(card_blocks)
        volatile_tokens = estimate_tokens(volatile_text)
        input_tokens = stable_tokens + semi_tokens + schema_tokens + volatile_tokens
        system_blocks = (*stable_blocks, *skill_blocks, *card_blocks)
        request = LLMRequest(
            system_blocks=list(system_blocks),
            messages=[LLMMessage(role=MessageRole.USER, text=volatile_text)],
            max_output_tokens=output_tokens,
            json_schema=json_schema,
        )
        return ContextPlan(
            request=request,
            stable_blocks=tuple(stable_blocks),
            semi_stable_blocks=(*skill_blocks, *card_blocks),
            volatile_text=volatile_text,
            prefix_hash=content_hash("\n\n".join(block.text for block in stable_blocks)),
            stable_tokens=stable_tokens,
            semi_stable_tokens=semi_tokens,
            volatile_tokens=volatile_tokens,
            input_tokens=input_tokens,
            max_input_tokens=limit,
            schema_tokens=schema_tokens,
            trims=tuple(trims),
        )

    # -- stable prefix -----------------------------------------------------
    def _bundle(self, task: Task) -> RuleBundle:
        """Rule bundle for the task's role."""
        if isinstance(self._rules, RuleBundle):
            return self._rules
        return self._rules.load(task.role)

    def _stable_blocks(self, bundle: RuleBundle, brief: Brief | None) -> list[SystemBlock]:
        """The cacheable prefix: rules, rubric excerpt, skill index, brief.

        Nothing iteration-, task- or section-specific may be added here: the whole point
        is that the same role reuses the same cached prefix for the entire run.
        """
        blocks = [SystemBlock(text=text, cache=True) for _, text in bundle.blocks()]
        blocks.append(SystemBlock(text=self._rubric_excerpt(brief), cache=True))
        index = self._skills.render_index()
        blocks.append(SystemBlock(text=f"## Skill index\n{index}", cache=True))
        blocks.append(SystemBlock(text=f"## Brief\n{_brief_text(brief)}", cache=True))
        return blocks

    def _rubric_excerpt(self, brief: Brief | None) -> str:
        """The rubric slice a role needs: sections, word ranges, gates, criteria."""
        lines = ["## Rubric excerpt"]
        if brief is None:
            lines.append("(no brief yet: the rubric applies once intake is complete)")
            return "\n".join(lines)
        spec = self._rubric.spec_for(brief.article_type)
        abstract = spec.abstract
        lines.append(f"article_type: {brief.article_type.value}")
        lines.append(f"abstract_words: {abstract.min_words}-{abstract.max_words}")
        lines.append("sections:")
        for section in resolve_sections(
            self._rubric, brief.article_type, extra_titles=brief.must_include
        ):
            upper = "unbounded" if section.max_words is None else str(section.max_words)
            required = "required" if section.required else "optional"
            lines.append(
                f"- {section.id}: {section.title} ({required}, {section.min_words}-{upper} words)"
            )
        lines.append(f"hard_gates: {', '.join(self._rubric.gate_names)}")
        criteria = ", ".join(
            f"{criterion.name} {criterion.weight:g}" for criterion in self._rubric.criteria
        )
        lines.append(f"criteria_weights: {criteria}")
        return "\n".join(lines)

    # -- semi-stable -------------------------------------------------------
    def _skill_blocks(self, selection: SkillSelection) -> list[SystemBlock]:
        """Selected skill bodies, one block per skill, in selection order."""
        return [
            SystemBlock(text=f"## Skill: {skill.name}\n{skill.body.strip()}")
            for skill in selection.skills
        ]

    def _task_cards(self, task: Task, run_state: GovernorRunState) -> list[SourceCard]:
        """Source cards the task names, in the task's own order (never the store's)."""
        wanted = list(task.source_ids)
        if not wanted:
            return []
        by_id = {card.id: card for card in run_state.sources}
        return [by_id[card_id] for card_id in wanted if card_id in by_id]

    @staticmethod
    def _card_blocks(cards: Sequence[SourceCard], *, with_notes: bool) -> list[SystemBlock]:
        """Source cards as compact JSON lines; ``with_notes`` adds the snippet."""
        if not cards:
            return []
        lines = ["## Source cards"]
        for card in cards:
            row: dict[str, Any] = {
                "id": card.id,
                "title": card.title,
                "year": card.year,
                "venue": card.venue,
                "doi": card.doi,
                "url": card.url,
                "verification": card.verification.value,
            }
            if with_notes:
                row["note"] = card.snippet
            lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        return [SystemBlock(text="\n".join(lines))]

    # -- volatile ----------------------------------------------------------
    @staticmethod
    def _task_text(task: Task) -> str:
        """The task as compact, sorted JSON: the instruction the model must follow."""
        return "## Task\n" + json.dumps(
            task.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _section_text(task: Task, run_state: GovernorRunState) -> str:
        """The text of every section the task names, verbatim, with its version."""
        if not task.section_ids:
            return ""
        states = {state.id: state for state in run_state.sections}
        parts: list[str] = []
        for section_id in task.section_ids:
            state = states.get(section_id)
            header = f"## Section text: {section_id}"
            if state is not None:
                header += f" (version {state.version}, hash {state.hash})"
            parts.append(f"{header}\n{_text_for_section(run_state, section_id)}")
        return "\n\n".join(parts)

    def _volatile_text(self, digest: str, task_text: str, section_text: str) -> str:
        """The user message: instruction, digest slice, task, section text."""
        parts = [TASK_INSTRUCTION]
        if digest:
            parts.append(f"## Digest (claude.md)\n{digest}")
        parts.append(task_text)
        if section_text:
            parts.append(section_text)
        return "\n\n".join(parts)


# --------------------------------------------------------------------------- helpers
def _skill_name(block_text: str) -> str:
    """Skill name from a rendered skill block header, for trim telemetry."""
    first = block_text.splitlines()[0] if block_text else ""
    return first.removeprefix("## Skill: ").strip() or "unknown"


def _tokens(blocks: Sequence[SystemBlock]) -> int:
    """Estimated tokens of a block list."""
    return sum(estimate_tokens(block.text) for block in blocks)


def _brief_text(brief: Brief | None) -> str:
    """The brief as compact JSON, or an explicit placeholder when intake is pending."""
    if brief is None:
        return "(no brief yet)"
    return json.dumps(brief.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _digest_of(run_state: GovernorRunState) -> str:
    """Read the optional digest slice from a run state, tolerating its absence."""
    digest = getattr(run_state, "digest", "")
    if not isinstance(digest, str):
        return ""
    return digest.strip()


def _text_for_section(run_state: GovernorRunState, section_id: str) -> str:
    """Look up a section body, tolerating either of the two documented shapes."""
    getter = getattr(run_state, "section_text", None)
    if callable(getter):
        text = getter(section_id)
        if isinstance(text, str) and text.strip():
            return text.strip()
    texts = getattr(run_state, "section_texts", None)
    if isinstance(texts, Mapping):
        value = texts.get(section_id)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(no text yet: this section has not been written)"


def _trim_digest_to(text: str, budget_tokens: int) -> str:
    """Keep the newest tail of the digest that fits ``budget_tokens``.

    Char budgeting makes this O(n): ``estimate_tokens`` is ``ceil(chars / 4)``, so a
    suffix of at most ``budget * 4 - len(marker)`` characters fits, and aligning the cut
    to a newline only shortens it further. Deterministic for a given digest and budget.
    """
    if budget_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    available_chars = budget_tokens * CHARS_PER_TOKEN - len(DIGEST_TRIM_MARKER)
    if available_chars <= 0:
        return ""
    tail = text[-available_chars:]
    newline = tail.find("\n")
    if newline != -1:
        tail = tail[newline + 1 :]
    if not tail.strip():
        return ""
    return DIGEST_TRIM_MARKER + tail
