"""The Planner: planning-only LLM layer (ADR-0001).

The Planner reads the bounded digest, section states, source references, the
latest quality report and the brief - never article bodies - and returns exactly
two things: a :class:`~app.contracts.plan.Plan` (every task owned by exactly one
agent role) and a :class:`~app.contracts.verdict.Verdict` (advice for the loop
controller, checked against deterministic report guardrails). Model output is
normalised server-side (plan version + ULID task ids, ADR-0008), validated
semantically, and repaired at most ``PlannerConfig.max_repairs`` times before the
Planner raises :class:`PlannerOutputError`.

Modules: :mod:`.context` (inputs), :mod:`.prompts` (cache-ordered prompt
assembly), :mod:`.validation` (pure parsing/guardrails), :mod:`.fallback`
(deterministic template plan + report verdict), :mod:`.planner` (the async
pipeline), :mod:`.errors`.
"""

from __future__ import annotations

from app.planner.context import ABSTRACT_SECTION_ID, PlannerContext
from app.planner.errors import PlannerContextTooLarge, PlannerError, PlannerOutputError
from app.planner.fallback import build_fallback_plan, verdict_from_report
from app.planner.planner import (
    BudgetGuard,
    Planner,
    PlannerConfig,
    PlannerPoolLike,
    PlanOutcome,
    PoolBackedProvider,
    VerdictOutcome,
)
from app.planner.prompts import (
    PLAN_JSON_SCHEMA,
    PLANNER_SYSTEM_PROMPT,
    VERDICT_JSON_SCHEMA,
    build_plan_request,
    build_repair_request,
    build_verdict_request,
)
from app.planner.validation import (
    apply_report_guardrails,
    missing_roles,
    parse_plan,
    parse_verdict,
    validate_plan_semantics,
)

__all__ = [
    "ABSTRACT_SECTION_ID",
    "PLANNER_SYSTEM_PROMPT",
    "PLAN_JSON_SCHEMA",
    "VERDICT_JSON_SCHEMA",
    "BudgetGuard",
    "PlanOutcome",
    "Planner",
    "PlannerConfig",
    "PlannerContext",
    "PlannerContextTooLarge",
    "PlannerError",
    "PlannerOutputError",
    "PlannerPoolLike",
    "PoolBackedProvider",
    "VerdictOutcome",
    "apply_report_guardrails",
    "build_fallback_plan",
    "build_plan_request",
    "build_repair_request",
    "build_verdict_request",
    "missing_roles",
    "parse_plan",
    "parse_verdict",
    "validate_plan_semantics",
    "verdict_from_report",
]
