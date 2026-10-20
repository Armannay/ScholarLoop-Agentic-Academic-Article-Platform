"""The Step-5 governor seam: enforce the documented caps before every provider call.

The full run/iteration governor (``app/governor/``) lands in a later step. Until then
:class:`CallBudgetGovernor` is the concrete implementation of the
:class:`~app.agents.base.Governor` protocol, and it enforces the two caps that exist
today:

* ``Policies.call_input_cap`` - the prompt ceiling for any single call, counting the
  JSON schema that is sent with the request;
* ``Task.token_budget`` - a single call may not project more tokens than the task was
  granted.

It refuses *before* the call, like the policy loader promises: a governor that
truncates silently is worse than one that says no. When the real governor arrives it
must satisfy the same ``precheck`` protocol; if it needs a different signature, that
is a CONTRACT-CHANGE-REQUEST for this seam, not a silent redefinition.
"""

from __future__ import annotations

import json

from app.contracts.enums import BindingRole
from app.contracts.llm import LLMRequest
from app.contracts.task import Task
from app.core.errors import BudgetExceeded
from app.core.policies import DEFAULT_POLICIES, Policies
from app.providers.base import estimate_tokens, prompt_tokens

__all__ = ["CallBudgetGovernor"]


class CallBudgetGovernor:
    """Deterministic pre-call budget check against the shipped policy values."""

    def __init__(self, policies: Policies = DEFAULT_POLICIES) -> None:
        self._policies = policies

    @property
    def policies(self) -> Policies:
        """The policy set this governor enforces."""
        return self._policies

    def precheck(self, request: LLMRequest, *, role: BindingRole, task: Task) -> None:
        """Raise :class:`BudgetExceeded` when the call cannot fit.

        Args:
            request: the exact request about to be sent.
            role: the binding role making the call (planner included, when used).
            task: the task whose ``token_budget`` bounds this call.
        """
        schema_tokens = 0
        if request.json_schema is not None:
            schema_text = json.dumps(request.json_schema, sort_keys=True, separators=(",", ":"))
            schema_tokens = estimate_tokens(schema_text)
        projected_input = prompt_tokens(request) + schema_tokens
        if projected_input > self._policies.call_input_cap:
            raise BudgetExceeded(
                f"call for role '{role.value}' projects {projected_input} input tokens; "
                f"call_input_cap is {self._policies.call_input_cap}",
                role=role.value,
                task_id=task.id,
                projected_input_tokens=projected_input,
                call_input_cap=self._policies.call_input_cap,
            )
        projected_total = projected_input + request.max_output_tokens
        if projected_total > task.token_budget:
            raise BudgetExceeded(
                f"call for role '{role.value}' could spend {projected_total} tokens; "
                f"task {task.id} budget is {task.token_budget}",
                role=role.value,
                task_id=task.id,
                projected_total_tokens=projected_total,
                task_token_budget=task.token_budget,
            )
