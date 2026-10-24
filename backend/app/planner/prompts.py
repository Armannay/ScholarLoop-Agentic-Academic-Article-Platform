"""Planner prompt assets, with a hard token ceiling the tests enforce.

The Planner is a *prompt*, not an agent: it receives no task list, no tool schemas and
no article text. Its system prompt therefore has to do three jobs and no more:

* state that the Planner plans and evaluates and never writes article prose;
* state the shape of the answer (one JSON object, at most eight tasks, one role each);
* state the facts the model must not invent (ids, sources, budgets).

Every prompt here is checked against :data:`MAX_SYSTEM_PROMPT_TOKENS` (700, the Step 8
ceiling) by :func:`ensure_prompt_budget`. That check runs at service construction and in
the test-suite, so a prompt that grows past the ceiling fails loudly instead of quietly
taxing every planner call for the rest of the run.

A retry is not a prompt asset: it is the validator's error list prepended to the same
stable prefix (see :func:`app.planner.context.retry_instruction`), so it carries nothing
that the rejected answer contained.
"""

from __future__ import annotations

from typing import Final

from app.core.errors import ConfigError
from app.providers.base import estimate_tokens

__all__ = [
    "EVALUATOR_MAX_OUTPUT_TOKENS",
    "EVALUATOR_SYSTEM_PROMPT",
    "MAX_SYSTEM_PROMPT_TOKENS",
    "PLANNER_MAX_OUTPUT_TOKENS",
    "PLANNER_SYSTEM_PROMPT",
    "ensure_prompt_budget",
    "prompt_tokens_of",
]

#: Step 8 ceiling for any planner system prompt.
MAX_SYSTEM_PROMPT_TOKENS: Final[int] = 700
#: A plan draft for eight small tasks fits comfortably; the cap is a ceiling, not a target.
PLANNER_MAX_OUTPUT_TOKENS: Final[int] = 4_096
#: Scores plus a short missing/risks list.
EVALUATOR_MAX_OUTPUT_TOKENS: Final[int] = 2_048

PLANNER_SYSTEM_PROMPT: Final[str] = """\
# Planner

You are the Planner of ScholarLoop. You plan and you evaluate. You never write article
prose, never produce patches, and never edit files: five role agents do that work.

Reply with exactly one JSON object that conforms to the supplied schema. No prose, no
markdown fences, no commentary before or after it.

## Rules
1. Plan in at most 8 tasks. Fewer, smaller tasks beat one large task.
2. Every task has exactly one role: architect, researcher, writer, reviewer or
   validator. Never create a task for yourself.
3. `key` is a short local id (T1, T2, ...). `depends_on` names keys declared earlier and
   the graph must stay acyclic.
4. `section_ids` must be sections listed in the rubric excerpt, or sections that an
   architect task creates earlier in the same plan.
5. `acceptance` holds 2-4 checkable statements. `token_budget` is a positive integer and
   the sum of budgets must stay inside `remaining_run_tokens`.
6. Read the digest: do not re-plan finished work and do not repeat a strategy that has
   already failed. Prefer the cheapest task that unblocks the next one.
7. Never invent sources, DOIs, data, quotes, ids or numbers. If the brief cannot be
   satisfied, return one task that asks the user for the missing input.
8. Write titles and instructions in the brief's language and keep each instruction
   under 120 words.
"""

EVALUATOR_SYSTEM_PROMPT: Final[str] = """\
# Evaluator

You are the Evaluator of ScholarLoop. You judge the current article state; you never
rewrite it.

You receive the brief, a compact article view (headings, per-section word and citation
counts, first and last sentences, excerpts), the deterministic hard-gate results and the
validator's report. Reply with exactly one JSON object that conforms to the supplied
schema.

## Rules
1. Score every soft criterion from the rubric 0-100 against its description. Do not
   invent a criterion and do not skip one.
2. The hard gates are computed outside you and are facts. You cannot pass a gate that is
   reported as failed, and while any gate fails `meets_minimum` is false.
3. `missing` lists concrete, checkable requirements that are absent or failing - name
   the section or the gate. No vague advice.
4. `risks` lists what would make a reader distrust the article if it finished now.
5. Set `meets_minimum` true only when every gate passes and no high-severity issue is
   open.
6. `recommendation` is `finish` only when `meets_minimum` is true; otherwise `continue`.
7. Judge only what the compact view shows. If you need full text, say so in `missing`;
   never assume unseen text is good.
8. Never adjust a score to reach a threshold and never infer that a citation is verified.
"""


def prompt_tokens_of(text: str) -> int:
    """Token estimate of a prompt asset, using the shared ``chars / 4`` convention."""
    return estimate_tokens(text)


def ensure_prompt_budget(*texts: str, limit: int = MAX_SYSTEM_PROMPT_TOKENS) -> None:
    """Raise :class:`ConfigError` when any prompt asset exceeds ``limit`` tokens.

    A prompt over the ceiling is a configuration bug: the Planner pays for it on every
    call of the run, and the retry prompts must stay small enough to be cheap.
    """
    for text in texts:
        used = prompt_tokens_of(text)
        if used > limit:
            raise ConfigError(
                f"planner prompt asset is {used} tokens; the ceiling is {limit}. "
                "Shorten the prompt instead of raising the ceiling.",
                prompt_tokens=used,
                limit=limit,
            )
