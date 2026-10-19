"""The Planner: a planning-only Claude client (ADR-0001).

The Planner returns exactly two shapes - a ``Plan`` (todo list plus rules,
every task owned by exactly one agent role) and a ``Verdict`` (advisory
evaluation). It never writes article text, reads only the bounded digest, and
runs on an Anthropic Console key (the mock in tests), enforced at the point
of use as well as in the account store.
"""

from __future__ import annotations

from app.planner.client import (
    ALLOWED_PLANNER_PROVIDERS,
    LLMPlanner,
    PlannerPool,
    PlannerPort,
    PlannerProvider,
)
from app.planner.errors import PlannerError, PlannerOutputError, PlannerProviderNotAllowed
from app.planner.prompts import (
    DEFAULT_PLANNER_MAX_OUTPUT_TOKENS,
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_ROLE,
    PLANNER_SYSTEM_PROMPT,
    build_evaluate_request,
    build_plan_request,
    load_planner_rules,
    plan_json_schema,
    verdict_json_schema,
)

__all__ = [
    "ALLOWED_PLANNER_PROVIDERS",
    "DEFAULT_PLANNER_MAX_OUTPUT_TOKENS",
    "EVALUATOR_SYSTEM_PROMPT",
    "PLANNER_ROLE",
    "PLANNER_SYSTEM_PROMPT",
    "LLMPlanner",
    "PlannerError",
    "PlannerOutputError",
    "PlannerPool",
    "PlannerPort",
    "PlannerProvider",
    "PlannerProviderNotAllowed",
    "build_evaluate_request",
    "build_plan_request",
    "load_planner_rules",
    "plan_json_schema",
    "verdict_json_schema",
]
