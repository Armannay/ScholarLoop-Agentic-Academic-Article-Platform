"""ScholarLoop governor: predictable, minimal token spend.

Five cooperating pieces (HARD RULE 3):

* :mod:`app.governor.budget` - nested budgets ``account-month -> run -> iteration ->
  call`` with reserve-then-settle accounting, a soft warning at 80 % and a hard stop at
  100 %;
* :mod:`app.governor.estimate` - :class:`TokenEstimator`, provider tokeniser or
  ``chars / 4`` plus a 15 % margin, with measured drift;
* :mod:`app.governor.context_builder` - the fixed prompt layout
  (stable prefix -> semi-stable references -> volatile task) and the documented trim
  order;
* :mod:`app.governor.telemetry` - per-call records, tokens by role/iteration,
  cache-hit ratio and token ROI;
* :mod:`app.governor.tiers` - ``(role, provider, binding) -> model`` via
  ``policies.model_tiers`` with per-agent override through ``AgentBinding``.

Everything here is deterministic and testable with ``MockProvider``; nothing in this
package opens a network connection.
"""

from __future__ import annotations

from app.governor.budget import (
    DEFAULT_SOFT_WARN,
    LEVEL_ORDER,
    BudgetEstimate,
    BudgetGovernor,
    BudgetLevel,
    BudgetLimits,
    BudgetScope,
    BudgetStatus,
    BudgetStore,
    BudgetStoreError,
    FileBudgetStore,
    InMemoryBudgetStore,
    LevelUsage,
    Reservation,
)
from app.governor.context_builder import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DIGEST_TRIM_MARKER,
    TASK_INSTRUCTION,
    TRIM_ORDER,
    ContextBuilder,
    ContextPlan,
    ContextTooLarge,
    GovernorRunState,
    MandatoryRuleGuard,
    MandatoryRuleRemoved,
    RuleBundle,
    RuleLibrary,
    TrimAction,
    TrimStep,
    mandatory_rules,
)
from app.governor.estimate import (
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_WINDOW,
    DriftReport,
    DriftSample,
    EstimateSource,
    TokenEstimate,
    TokenEstimator,
)
from app.governor.telemetry import (
    DEFAULT_MAX_RECORDS,
    CallRecord,
    QualityPoint,
    TelemetryError,
    TelemetryRecorder,
    TelemetrySummary,
    TokenROI,
    UsageTotals,
)
from app.governor.tiers import (
    DEFAULT_MODELS,
    ModelChoice,
    ModelSource,
    TierResolver,
    as_binding_role,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_MODELS",
    "DEFAULT_SAFETY_MARGIN",
    "DEFAULT_SOFT_WARN",
    "DEFAULT_WINDOW",
    "DIGEST_TRIM_MARKER",
    "LEVEL_ORDER",
    "TASK_INSTRUCTION",
    "TRIM_ORDER",
    "BudgetEstimate",
    "BudgetGovernor",
    "BudgetLevel",
    "BudgetLimits",
    "BudgetScope",
    "BudgetStatus",
    "BudgetStore",
    "BudgetStoreError",
    "CallRecord",
    "ContextBuilder",
    "ContextPlan",
    "ContextTooLarge",
    "DriftReport",
    "DriftSample",
    "EstimateSource",
    "FileBudgetStore",
    "GovernorRunState",
    "InMemoryBudgetStore",
    "LevelUsage",
    "MandatoryRuleGuard",
    "MandatoryRuleRemoved",
    "ModelChoice",
    "ModelSource",
    "QualityPoint",
    "Reservation",
    "RuleBundle",
    "RuleLibrary",
    "TelemetryError",
    "TelemetryRecorder",
    "TelemetrySummary",
    "TierResolver",
    "TokenEstimate",
    "TokenEstimator",
    "TokenROI",
    "TrimAction",
    "TrimStep",
    "UsageTotals",
    "as_binding_role",
    "mandatory_rules",
]
