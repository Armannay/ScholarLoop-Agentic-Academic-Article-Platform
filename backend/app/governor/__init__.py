"""Governor: nested token budgets, estimates, prompt assembly, telemetry, tiers.

The governor is the deterministic loop's cost-awareness layer. Nothing here calls
a provider; the orchestrator asks it three questions and obeys the answers:

* *May I spend this?* - :class:`BudgetManager.precheck` / :meth:`BudgetManager.record`
* *How big is this?* - :class:`TokenEstimator`
* *What prompt do I send?* - :func:`build` / :class:`ContextBuilder`

and records the outcome in :class:`TelemetryStore`. :func:`resolve_model` turns a
policy tier or a user binding into a concrete model id.
"""

from __future__ import annotations

from app.governor.budget import (
    BudgetExceeded,
    BudgetLevel,
    BudgetManager,
    BudgetSnapshot,
    BudgetStatus,
    BudgetWarning,
    HierarchicalBudget,
    month_key,
)
from app.governor.context_builder import (
    ContextBuilder,
    ContextTooLarge,
    MandatoryRuleViolation,
    RunState,
    SkillInfo,
    SkillRef,
    build,
    build_stable_prefix,
    default_role_rules,
    default_root_rules,
    mandatory_rules,
    merge_rules,
    validate_mandatory_rules,
)
from app.governor.estimate import (
    DriftRecord,
    DriftStats,
    TokenEstimator,
    estimate_with_margin,
)
from app.governor.telemetry import (
    CallRecord,
    TelemetryStore,
    cache_hit_ratio_for,
    token_roi,
    token_roi_from_reports,
)
from app.governor.tiers import (
    DEFAULT_TIER_MODELS,
    describe_resolution,
    resolve_model,
    resolve_tier,
    tier_for_role,
)

__all__ = [
    "DEFAULT_TIER_MODELS",
    "BudgetExceeded",
    "BudgetLevel",
    "BudgetManager",
    "BudgetSnapshot",
    "BudgetStatus",
    "BudgetWarning",
    "CallRecord",
    "ContextBuilder",
    "ContextTooLarge",
    "DriftRecord",
    "DriftStats",
    "HierarchicalBudget",
    "MandatoryRuleViolation",
    "RunState",
    "SkillInfo",
    "SkillRef",
    "TelemetryStore",
    "TokenEstimator",
    "build",
    "build_stable_prefix",
    "cache_hit_ratio_for",
    "default_role_rules",
    "default_root_rules",
    "describe_resolution",
    "estimate_with_margin",
    "mandatory_rules",
    "merge_rules",
    "month_key",
    "resolve_model",
    "resolve_tier",
    "tier_for_role",
    "token_roi",
    "token_roi_from_reports",
    "validate_mandatory_rules",
]
