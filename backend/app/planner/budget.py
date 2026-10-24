"""The budget a plan is allowed to spend, and the token allowance per call.

``PlanBudget`` is the number the orchestrator hands the Planner at an event: how many
run tokens are left, which iteration this is, and how many tasks the iteration may
contain. It is deliberately *not* a shadow of the governor: the governor (Step 2)
enforces the nested caps at call time; this value only bounds the plan the Planner
writes, so a plan that is valid in shape but unaffordable is rejected before any agent
starts working against it.

Two ceilings matter here:

* ``remaining_run_tokens`` - the sum of task ``token_budget`` values may not exceed it;
* ``max_tasks`` - the per-iteration task cap, hard-limited to
  :data:`MAX_TASKS_PER_ITERATION` (8) by the step brief.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.policies import DEFAULT_POLICIES, Policies

__all__ = ["MAX_TASKS_PER_ITERATION", "PlanBudget"]

#: Hard ceiling from the Step 8 brief: one iteration carries at most eight tasks.
MAX_TASKS_PER_ITERATION: Final[int] = 8


@dataclass(frozen=True)
class PlanBudget:
    """What the next plan may spend.

    Args:
        remaining_run_tokens: tokens still available in the run. The sum of the
            tasks' ``token_budget`` values must not exceed it.
        iteration: loop iteration the plan belongs to (ledger/digest arithmetic).
        max_tasks: per-iteration task cap; never above 8.
        call_token_budget: optional ceiling for *one* planner call (all retries
            included). ``None`` means "bounded only by the governor".
    """

    remaining_run_tokens: int
    iteration: int = 0
    max_tasks: int = MAX_TASKS_PER_ITERATION
    call_token_budget: int | None = None

    def __post_init__(self) -> None:
        if self.remaining_run_tokens < 1:
            raise ValueError(f"remaining_run_tokens must be >= 1, got {self.remaining_run_tokens}")
        if not 0 <= self.iteration <= 10_000:
            raise ValueError(f"iteration must be 0..10000, got {self.iteration}")
        if not 1 <= self.max_tasks <= MAX_TASKS_PER_ITERATION:
            raise ValueError(
                f"max_tasks must be 1..{MAX_TASKS_PER_ITERATION} "
                f"(the per-iteration cap), got {self.max_tasks}"
            )
        if self.call_token_budget is not None and self.call_token_budget < 1:
            raise ValueError(
                f"call_token_budget must be >= 1 when set, got {self.call_token_budget}"
            )

    @classmethod
    def from_policies(
        cls,
        policies: Policies = DEFAULT_POLICIES,
        *,
        spent_tokens: int = 0,
        iteration: int = 0,
        call_token_budget: int | None = None,
    ) -> PlanBudget:
        """Build the budget for ``iteration`` from the shipped policy caps.

        ``spent_tokens`` is the run's committed token usage; the remainder is what the
        plan may allocate. When the run is already spent the budget bottoms out at one
        token rather than zero: a plan with no budget is indistinguishable from a
        rejected plan, and the loop controller - not this class - owns the stop.
        """
        if spent_tokens < 0:
            raise ValueError(f"spent_tokens must be >= 0, got {spent_tokens}")
        remaining = max(1, policies.run_token_cap - spent_tokens)
        return cls(
            remaining_run_tokens=remaining,
            iteration=iteration,
            call_token_budget=call_token_budget,
        )

    def allows(self, total_task_tokens: int) -> bool:
        """True when a plan asking for ``total_task_tokens`` fits this budget."""
        return total_task_tokens <= self.remaining_run_tokens

    def describe(self) -> dict[str, int]:
        """The numbers shown to the Planner, so it budgets against facts not guesses."""
        described = {
            "remaining_run_tokens": self.remaining_run_tokens,
            "max_tasks": self.max_tasks,
            "iteration": self.iteration,
        }
        if self.call_token_budget is not None:
            described["call_token_budget"] = self.call_token_budget
        return described
