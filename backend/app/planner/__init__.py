"""The Planner / Evaluator: planning and judgement, never authoring.

The Planner is the only LLM role that is not an agent. It receives the brief and the
bounded ``claude.md`` digest and returns one of three things:

* a :class:`~app.contracts.plan.Plan` - a todo list where every task belongs to exactly
  one of the five agent roles (never to the planner itself);
* a :class:`~app.planner.plan_validator.PlanDelta` - the tasks to add and the tasks to
  drop, so replanning costs a delta instead of a rewrite;
* a :class:`~app.contracts.verdict.Verdict` - a judgement that the deterministic hard
  gates can only make stricter.

It never writes article text, never produces patches and never touches the article
store. Everything it spends goes through the Step 2 governor and every prompt is built
by the Step 2 :class:`~app.governor.context_builder.ContextBuilder` (see
:mod:`app.planner.context` for the Task-shaped adapter and why one is needed).

Modules:

* :mod:`app.planner.service` - ``plan`` / ``replan`` / ``evaluate``;
* :mod:`app.planner.plan_validator` - deterministic plan and delta validation;
* :mod:`app.planner.probes` - the 8 hard gates and the compact article view;
* :mod:`app.planner.evaluation` - merging model judgement with deterministic gates;
* :mod:`app.planner.events` - when a planner call is allowed at all;
* :mod:`app.planner.context` - prompt assembly, the synthetic task, retry messages;
* :mod:`app.planner.budget` - :class:`~app.planner.budget.PlanBudget`;
* :mod:`app.planner.drafts` - the JSON shapes the model actually returns;
* :mod:`app.planner.prompts` - prompt assets under a 700-token ceiling;
* :mod:`app.planner.errors` - the planner error family.
"""

from __future__ import annotations

from app.planner.budget import MAX_TASKS_PER_ITERATION, PlanBudget
from app.planner.context import (
    EVALUATE_INSTRUCTION,
    PLAN_INSTRUCTION,
    REPLAN_INSTRUCTION,
    PlannerContexts,
    PlannerPromptBuilder,
    PlannerRequest,
    PlannerRunState,
    build_planner_contexts,
    default_planner_context_builder,
    planner_rule_bundle,
    planner_task,
    retry_instruction,
)
from app.planner.drafts import (
    DraftTask,
    EvaluationDraft,
    PlanDeltaDraft,
    PlanDraft,
    evaluation_schema,
    plan_delta_schema,
    plan_draft_schema,
)
from app.planner.errors import (
    EvaluationRejected,
    PlannerError,
    PlannerOutputInvalid,
    PlannerRejected,
    PlannerStateMissing,
    PlannerUnavailable,
    PlanRejected,
)
from app.planner.evaluation import (
    Evaluation,
    criteria_names,
    evaluation_problems,
    merge_verdict,
    weighted_q,
)
from app.planner.events import PLATEAU_REASON, PlannerCall, PlannerCallPolicy, PlannerEvent
from app.planner.plan_validator import (
    MAX_TASKS_PER_PLAN,
    VALID_PLAN_ROLES,
    PlanDelta,
    PlanValidator,
    plan_role_order,
)
from app.planner.probes import (
    DEFAULT_DEEP_CHECK_TOKENS,
    MAX_EDGE_SENTENCE_TOKENS,
    MAX_EXCERPT_TOKENS,
    MAX_EXCERPTS,
    ArticleView,
    CompactArticleView,
    Excerpt,
    GateResult,
    ProbeReport,
    SectionSummary,
    build_compact_view,
    build_probe_report,
    gate_results,
    split_sentences,
)
from app.planner.prompts import (
    EVALUATOR_MAX_OUTPUT_TOKENS,
    EVALUATOR_SYSTEM_PROMPT,
    MAX_SYSTEM_PROMPT_TOKENS,
    PLANNER_MAX_OUTPUT_TOKENS,
    PLANNER_SYSTEM_PROMPT,
    ensure_prompt_budget,
)
from app.planner.service import MAX_PLANNER_RETRIES, PlannerService

__all__ = [
    "DEFAULT_DEEP_CHECK_TOKENS",
    "EVALUATE_INSTRUCTION",
    "EVALUATOR_MAX_OUTPUT_TOKENS",
    "EVALUATOR_SYSTEM_PROMPT",
    "MAX_EDGE_SENTENCE_TOKENS",
    "MAX_EXCERPTS",
    "MAX_EXCERPT_TOKENS",
    "MAX_PLANNER_RETRIES",
    "MAX_SYSTEM_PROMPT_TOKENS",
    "MAX_TASKS_PER_ITERATION",
    "MAX_TASKS_PER_PLAN",
    "PLANNER_MAX_OUTPUT_TOKENS",
    "PLANNER_SYSTEM_PROMPT",
    "PLAN_INSTRUCTION",
    "PLATEAU_REASON",
    "REPLAN_INSTRUCTION",
    "VALID_PLAN_ROLES",
    "ArticleView",
    "CompactArticleView",
    "DraftTask",
    "Evaluation",
    "EvaluationDraft",
    "EvaluationRejected",
    "Excerpt",
    "GateResult",
    "PlanBudget",
    "PlanDelta",
    "PlanDeltaDraft",
    "PlanDraft",
    "PlanRejected",
    "PlanValidator",
    "PlannerCall",
    "PlannerCallPolicy",
    "PlannerContexts",
    "PlannerError",
    "PlannerEvent",
    "PlannerOutputInvalid",
    "PlannerPromptBuilder",
    "PlannerRejected",
    "PlannerRequest",
    "PlannerRunState",
    "PlannerService",
    "PlannerStateMissing",
    "PlannerUnavailable",
    "ProbeReport",
    "SectionSummary",
    "build_compact_view",
    "build_planner_contexts",
    "build_probe_report",
    "criteria_names",
    "default_planner_context_builder",
    "ensure_prompt_budget",
    "evaluation_problems",
    "evaluation_schema",
    "gate_results",
    "merge_verdict",
    "plan_delta_schema",
    "plan_draft_schema",
    "plan_role_order",
    "planner_rule_bundle",
    "planner_task",
    "retry_instruction",
    "split_sentences",
    "weighted_q",
]
