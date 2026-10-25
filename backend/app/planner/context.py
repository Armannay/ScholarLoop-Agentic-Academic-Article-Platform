"""Planner prompt assembly on top of the existing governor :class:`ContextBuilder`.

The Planner is not an agent: CONTRACTS v1 gives it a ``BindingRole`` but no
``AgentRole``, so ``ContextBuilder`` - which is deliberately task-shaped - has no
planner entry point. This module is the adapter, and it is an adapter *only*: the layout
(rules -> rubric excerpt -> skill index -> brief -> digest -> task), the trim order, the
mandatory-rule guard and the token accounting all stay in
:mod:`app.governor.context_builder`. Nothing here re-implements them.

Two facts about the adapter are worth stating, because they are visible in the prompt:

1. **The rules come from a fixed bundle.** ``RuleBundle(root=<root rules>,
   role=<planner system prompt>)`` is passed to the builder, so the planner's stable
   prefix is the mandatory root rules plus the planner prompt - never an agent role's
   rules file. The mandatory-rule guard still runs, so a project cannot strip a root
   rule from a planner call either.
2. **The synthetic task names a real ``AgentRole``.** ``Task.role`` is an enum without a
   planner member, so the adapter names the closest role purely to select the rule/skill
   path: ``architect`` for planning (structure) and ``validator`` for evaluation
   (judgement). The task's own instructions say, in the prompt, that this is a planner
   call and that no article prose may be produced. The synthetic task never enters a
   :class:`~app.contracts.plan.Plan` and is never executed by an agent.

The agent runtime's instruction line (``TASK_INSTRUCTION``, which asks for an
``AgentResult`` with patches) is replaced by the planner instruction. That is the one
textual rewrite the adapter performs, and it is why
:meth:`PlannerPromptBuilder.build` re-measures the request: the final prompt, not the
builder's estimate, is what has to fit the binding's cap.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, MessageRole
from app.contracts.llm import LLMMessage, LLMRequest
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.ids import new_ulid
from app.core.rubric import Rubric
from app.governor.context_builder import (
    TASK_INSTRUCTION,
    ContextBuilder,
    ContextPlan,
    ContextTooLarge,
    RuleBundle,
)
from app.planner.budget import PlanBudget
from app.planner.prompts import (
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    ensure_prompt_budget,
)
from app.providers.base import estimate_tokens, prompt_tokens
from app.skills.loader import SkillLoader

__all__ = [
    "EVALUATE_INSTRUCTION",
    "MAX_FIT_ATTEMPTS",
    "PLANNER_PROMPT_SLACK_TOKENS",
    "PLAN_INSTRUCTION",
    "REPLAN_INSTRUCTION",
    "PlannerContexts",
    "PlannerPromptBuilder",
    "PlannerRequest",
    "PlannerRunState",
    "build_planner_contexts",
    "default_planner_context_builder",
    "planner_rule_bundle",
    "planner_task",
    "retry_instruction",
]

#: How many times the adapter may shrink the builder's limit before giving up.
MAX_FIT_ATTEMPTS: Final[int] = 6
#: Headroom kept on each shrink so the next attempt is strictly tighter.
PLANNER_PROMPT_SLACK_TOKENS: Final[int] = 8

PLAN_INSTRUCTION: Final[str] = (
    "Produce the plan for this brief. You are the Planner, not an executing agent: "
    "return only the plan JSON."
)
REPLAN_INSTRUCTION: Final[str] = (
    "Revise the plan at this event. Return only the delta JSON: the tasks to add, the "
    "task ids to remove, and the reason."
)
EVALUATE_INSTRUCTION: Final[str] = (
    "Evaluate the article state against the brief and the rubric. The hard-gate results "
    "are facts; you cannot overturn them. Return only the evaluation JSON."
)


def retry_instruction(problems: Sequence[str]) -> str:
    """The retry message: the validator's error list, and nothing else.

    No previous plan, no previous evaluation, no article text and no conversation are
    re-sent. A retry is cheap by construction, which is what makes "retry at most twice"
    an affordable policy.
    """
    lines = ["The previous answer failed deterministic validation. Errors:"]
    lines.extend(f"- {problem}" for problem in problems)
    lines.append("")
    lines.append("Return only the corrected JSON object that fixes every error above.")
    return "\n".join(lines)


@dataclass(frozen=True)
class PlannerRunState:
    """What the planner call reads from the run.

    Satisfies :class:`app.governor.context_builder.GovernorRunState` structurally:
    ``iter``, ``brief``, ``sections`` and ``sources`` are required; ``digest`` is the
    bounded ``claude.md`` slice the builder trims when the prompt is tight.
    """

    brief: Brief | None
    iter: int = 0
    digest: str = ""
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.iter <= 10_000:
            raise ValueError(f"iter must be 0..10000, got {self.iter}")


def planner_rule_bundle(root_rules: str, system_prompt: str) -> RuleBundle:
    """The fixed rule bundle for planner calls: root rules plus the planner prompt.

    Raises:
        ConfigError: the root rules are blank - a planner without the hard rules is as
            invalid as an agent without them.
    """
    if not root_rules.strip():
        raise ConfigError("root rules must not be empty; a planner needs the hard rules")
    return RuleBundle(root=root_rules, role=system_prompt)


def default_planner_context_builder(
    *,
    rules_dir: Path | str,
    rubric: Rubric,
    skills: SkillLoader,
    system_prompt: str,
    max_input_tokens: int,
    max_output_tokens: int,
) -> ContextBuilder:
    """Build the planner's :class:`ContextBuilder` from the repository assets.

    Args:
        rules_dir: ``rules/`` - only ``root.rules.md`` is read; no agent role file is
            used for a planner call.
        rubric: the loaded :class:`~app.core.rubric.Rubric`.
        skills: the central :class:`~app.skills.loader.SkillLoader` (index + selection).
        system_prompt: the planner system prompt, checked against the 700-token ceiling.
        max_input_tokens: default prompt ceiling for planner calls.
        max_output_tokens: default completion ceiling for planner calls.

    Raises:
        ConfigError: the root rules file is missing, or the prompt is over budget.
    """
    ensure_prompt_budget(system_prompt)
    root_path = Path(rules_dir) / "root.rules.md"
    if not root_path.is_file():
        raise ConfigError(f"rules file not found: {root_path}")
    return ContextBuilder(
        rules=planner_rule_bundle(root_path.read_text(encoding="utf-8"), system_prompt),
        rubric=rubric,
        skills=skills,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )


def build_planner_contexts(
    *,
    rules_dir: Path | str,
    rubric: Rubric,
    skills: SkillLoader,
    max_input_tokens: int,
    max_output_tokens: int,
) -> PlannerContexts:
    """Build the two planner contexts: one for planning, one for evaluation.

    Two builders exist because the *role block* of the stable prefix is the system
    prompt, and planning and evaluating need different instructions. Both share the same
    root rules, rubric excerpt, skill index and brief, so the cache prefix is identical
    apart from that block.
    """
    planning = default_planner_context_builder(
        rules_dir=rules_dir,
        rubric=rubric,
        skills=skills,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    evaluation = default_planner_context_builder(
        rules_dir=rules_dir,
        rubric=rubric,
        skills=skills,
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    return PlannerContexts(planning=planning, evaluation=evaluation)


@dataclass(frozen=True)
class PlannerContexts:
    """The :class:`ContextBuilder` instances the Planner uses, by purpose."""

    planning: ContextBuilder
    evaluation: ContextBuilder

    def for_kind(self, kind: str) -> ContextBuilder:
        """The builder for a planner call kind (``plan``/``replan``/``evaluate``)."""
        return self.evaluation if kind == EVALUATE else self.planning


#: Call kinds, so the service never passes a loose string around.
PLAN: Final[str] = "plan"
REPLAN: Final[str] = "replan"
EVALUATE: Final[str] = "evaluate"


def planner_task(
    *,
    purpose: str,
    instructions: str,
    budget: PlanBudget,
    role: AgentRole = AgentRole.ARCHITECT,
    id_factory: Callable[[], str] = new_ulid,
) -> Task:
    """The synthetic task that describes one planner call to the context builder.

    It is a *description*, not work: no agent executes it and it never appears in a
    ``Plan``. ``token_budget`` is the allowance for this call so the governor's
    ``Task.token_budget`` check bounds a planner call exactly like an agent call.
    """
    allowance = budget.call_token_budget or budget.remaining_run_tokens
    return Task(
        id=id_factory(),
        title=f"Planner call: {purpose}"[:120],
        role=role,
        section_ids=[],
        source_ids=[],
        instructions=(
            f"{instructions} This is a planner call ({purpose}): produce planning or "
            "evaluation JSON only, never article prose, never patches."
        )[:2400],
        acceptance=[
            "Return exactly one JSON object that conforms to the supplied schema.",
            "Spend no more than the stated token budget.",
        ],
        token_budget=allowance,
        depends_on=[],
    )


@dataclass(frozen=True)
class PlannerRequest:
    """One fully measured planner request plus the numbers the caps are checked against."""

    request: LLMRequest
    context_plan: ContextPlan
    input_tokens: int
    schema_tokens: int
    max_input_tokens: int
    max_output_tokens: int

    @property
    def within_cap(self) -> bool:
        """True when the measured prompt fits the ceiling it was built for."""
        return self.input_tokens <= self.max_input_tokens


class PlannerPromptBuilder:
    """Turn a task and a run state into a planner-shaped, measured request.

    The class is a thin wrapper: every byte of layout, trimming and accounting comes from
    the injected :class:`~app.governor.context_builder.ContextBuilder`.
    """

    def __init__(self, context_builder: ContextBuilder) -> None:
        self._context_builder = context_builder

    @property
    def context_builder(self) -> ContextBuilder:
        """The wrapped builder, exposed so callers can share one instance per run."""
        return self._context_builder

    def build(
        self,
        *,
        task: Task,
        run_state: PlannerRunState,
        instruction: str,
        json_schema: dict[str, Any],
        max_input_tokens: int,
        max_output_tokens: int,
        volatile_override: str | None = None,
        append: str | None = None,
    ) -> PlannerRequest:
        """Assemble the request and prove it fits ``max_input_tokens``.

        The builder is asked to plan against the same ceiling; because the planner
        instruction replaces the agent runtime's instruction line, the finished request
        is measured again and the builder is re-run with a tighter ceiling if the
        rewrite made it longer. Shrinking really does help - the builder trims the digest
        to fit - so the loop converges. A ``volatile_override`` (the retry message) is
        fixed text, so the loop stops as soon as it does not fit. Nothing is ever
        truncated silently: the builder keeps ownership of the trim order.

        Raises:
            ContextTooLarge: the prompt cannot fit even at engine minimum, or the
                rewrite pushed it over every ceiling the builder can honour.
        """
        schema_text = json.dumps(json_schema, sort_keys=True, separators=(",", ":"))
        schema_tokens = estimate_tokens(schema_text)
        limit = max_input_tokens
        last_needed = 0
        for _ in range(MAX_FIT_ATTEMPTS):
            context_plan = self._context_builder.plan(
                task,
                run_state,
                max_input_tokens=limit,
                max_output_tokens=max_output_tokens,
                json_schema=json_schema,
            )
            if volatile_override is not None:
                text = volatile_override
            else:
                text = _with_instruction(context_plan.volatile_text, instruction)
                if append:
                    text = f"{text}\n\n{append}"
            request = LLMRequest(
                system_blocks=[*context_plan.stable_blocks, *context_plan.semi_stable_blocks],
                messages=[LLMMessage(role=MessageRole.USER, text=text)],
                max_output_tokens=max_output_tokens,
                json_schema=json_schema,
            )
            needed = prompt_tokens(request) + schema_tokens
            if needed <= limit:
                return PlannerRequest(
                    request=request,
                    context_plan=context_plan,
                    input_tokens=needed,
                    schema_tokens=schema_tokens,
                    max_input_tokens=max_input_tokens,
                    max_output_tokens=max_output_tokens,
                )
            last_needed = needed
            if volatile_override is not None:
                # Fixed text: the builder cannot shrink it, so stop instead of looping.
                break
            tighter = limit - (needed - limit) - PLANNER_PROMPT_SLACK_TOKENS
            if tighter < 1 or tighter >= limit:
                break
            limit = tighter

        raise ContextTooLarge(
            f"the planner prompt needs {last_needed} input tokens; the ceiling is "
            f"{max_input_tokens}. Nothing the model must obey is truncated, so the call "
            "is refused instead.",
            input_tokens=last_needed,
            max_input_tokens=max_input_tokens,
            role="planner",
            task_id=task.id,
        )


def _with_instruction(volatile_text: str, instruction: str) -> str:
    """Swap the agent runtime's instruction for the planner instruction.

    ``ContextBuilder`` always prefixes ``TASK_INSTRUCTION`` (an ``AgentResult`` with
    patches). The planner must not be told to produce patches, so the prefix is replaced
    once, at the head of the message, and the rest of the layout is untouched.
    """
    body = volatile_text
    if body.startswith(TASK_INSTRUCTION):
        body = body[len(TASK_INSTRUCTION) :].lstrip("\n")
    if not body.strip():
        return instruction
    return f"{instruction}\n\n{body}"
