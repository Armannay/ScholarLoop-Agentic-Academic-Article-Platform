"""The token governor: refuse over-budget calls, never truncate them.

Three caps, taken from the policy file (see ``config/policies.yaml``):

* ``call_input_cap`` - the ceiling for the *prompt* of a single call;
* ``iteration_token_cap`` - the ceiling for one loop iteration;
* ``run_token_cap`` - the ceiling for the whole run.

The governor's rule is one sentence: **it refuses, it never truncates**. A prompt
that does not fit is an error the caller has to handle (shorten the context, split
the task, stop the run) - silently trimming a prompt would produce a half-answered
question and an article that nobody can explain.

The governor is also where a call's *estimated* cost is compared with what is left.
An estimate is ``prompt tokens + max_output_tokens``: the completion cannot be
known in advance, so the budget is reserved for the worst case and reconciled
against the real usage when the response arrives (:meth:`TokenGovernor.charge`).

The counters here are authoritative. A caller may pass a ``run_state`` object (the
orchestrator's run state) so that per-iteration and per-task budgets that live
there are honoured as well; the governor reads them, it never owns them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import Field

from app.contracts.binding import AgentBinding
from app.contracts.common import Contract
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.core.errors import BudgetExceeded
from app.core.policies import Policies
from app.providers.base import prompt_tokens

__all__ = ["BUDGET_CAPS", "Precheck", "TokenGovernor"]


class Precheck(Contract):
    """What the governor decided about one call, and what it cost to get there.

    The numbers are what the ledger and the loop controller need to explain a
    decision ("why was this call refused?") without re-deriving anything.
    """

    prompt_tokens: int = Field(ge=0, description="Estimated prompt size of the call.")
    max_output_tokens: int = Field(ge=0, description="Output cap declared by the request.")
    estimated_total: int = Field(ge=0, description="prompt + max_output.")
    model: str = Field(description="Model the call will use.")
    account_id: str | None = Field(default=None, description="Account the call will be billed to.")


def _read_state(run_state: object, *names: str) -> Any:
    """Read the first present key/attribute from a run state.

    The run state is deliberately not a contract yet: the orchestrator owns it and
    may evolve it. Reading it leniently (mapping or attributes) keeps the governor
    usable with whatever shape the orchestrator has, without importing it.
    """
    for name in names:
        if isinstance(run_state, Mapping):
            if name in run_state:
                return run_state[name]
            continue
        value = getattr(run_state, name, None)
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> int | None:
    """Coerce a state value to a non-negative int, or ``None`` when unusable."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


class TokenGovernor:
    """Tracks token spend and refuses calls the policies do not allow.

    The governor is cheap and synchronous: it never touches the network and never
    calls a provider, so it can be exercised in a unit test with exact numbers.
    """

    def __init__(self, policies: Policies) -> None:
        self._policies = policies
        self._run_used = 0
        self._iteration_used = 0
        self._iteration = 0
        self._account_usage: dict[str, int] = {}

    # -- state -------------------------------------------------------------
    @property
    def policies(self) -> Policies:
        """The policies this governor enforces."""
        return self._policies

    @property
    def run_used(self) -> int:
        """Tokens charged to the whole run so far."""
        return self._run_used

    @property
    def iteration_used(self) -> int:
        """Tokens charged to the current iteration so far."""
        return self._iteration_used

    @property
    def iteration(self) -> int:
        """Index of the current iteration."""
        return self._iteration

    @property
    def run_remaining(self) -> int:
        """Tokens the run may still spend."""
        return max(0, self._policies.run_token_cap - self._run_used)

    @property
    def iteration_remaining(self) -> int:
        """Tokens the current iteration may still spend."""
        return max(0, self._policies.iteration_token_cap - self._iteration_used)

    @property
    def over_budget(self) -> bool:
        """True when the run already spent more than its cap allows.

        A single call can overshoot the cap (the completion was longer than
        reserved); the governor records the overrun instead of pretending it did
        not happen, and the loop controller stops the run.
        """
        return self._run_used > self._policies.run_token_cap

    def account_used(self, account_id: str) -> int:
        """Tokens charged to one account so far."""
        return self._account_usage.get(account_id, 0)

    def begin_iteration(self, index: int) -> None:
        """Start a new iteration: the per-iteration counter restarts at zero."""
        self._iteration = max(0, index)
        self._iteration_used = 0

    # -- decisions ---------------------------------------------------------
    def precheck(
        self,
        request: LLMRequest,
        binding: AgentBinding,
        run_state: object = None,
    ) -> Precheck:
        """Decide whether this call may proceed.

        Args:
            request: the call about to be made.
            binding: the role's binding, which carries its own token ceilings.
            run_state: optional run state; when it exposes iteration or task
                budgets, they are honoured as well.

        Returns:
            The :class:`Precheck` describing the estimated cost of the call.

        Raises:
            BudgetExceeded: the call does not fit into one of the caps. The error
                names the cap in its context, so the loop can log one stable code.
        """
        prompt = prompt_tokens(request)
        estimated = prompt + request.max_output_tokens

        if prompt > self._policies.call_input_cap:
            raise BudgetExceeded(
                "the prompt exceeds the per-call input cap",
                cap="call_input_cap",
                limit=self._policies.call_input_cap,
                requested=prompt,
            )

        if prompt > binding.max_input_tokens:
            raise BudgetExceeded(
                "the prompt exceeds the binding's input ceiling",
                cap="binding_max_input_tokens",
                limit=binding.max_input_tokens,
                requested=prompt,
                role=binding.role.value,
            )

        if request.max_output_tokens > binding.max_output_tokens:
            raise BudgetExceeded(
                "the requested output exceeds the binding's output ceiling",
                cap="binding_max_output_tokens",
                limit=binding.max_output_tokens,
                requested=request.max_output_tokens,
                role=binding.role.value,
            )

        iteration_used = max(
            self._iteration_used,
            _as_int(_read_state(run_state, "iteration_tokens", "iteration_tokens_used")) or 0,
        )
        if iteration_used + estimated > self._policies.iteration_token_cap:
            raise BudgetExceeded(
                "the call does not fit into the iteration's token cap",
                cap="iteration_token_cap",
                limit=self._policies.iteration_token_cap,
                used=iteration_used,
                requested=estimated,
            )

        run_used = max(
            self._run_used,
            _as_int(_read_state(run_state, "run_tokens", "run_tokens_used")) or 0,
        )
        if run_used + estimated > self._policies.run_token_cap:
            raise BudgetExceeded(
                "the call does not fit into the run's token cap",
                cap="run_token_cap",
                limit=self._policies.run_token_cap,
                used=run_used,
                requested=estimated,
            )

        task_budget = _as_int(_read_state(run_state, "task_token_budget"))
        task_used = _as_int(_read_state(run_state, "task_tokens_used"))
        if (
            task_budget is not None
            and task_used is not None
            and task_used + estimated > task_budget
        ):
            raise BudgetExceeded(
                "the call does not fit into the task's token budget",
                cap="task_token_budget",
                limit=task_budget,
                used=task_used,
                requested=estimated,
            )

        return Precheck(
            prompt_tokens=prompt,
            max_output_tokens=request.max_output_tokens,
            estimated_total=estimated,
            model=binding.model,
        )

    def charge(self, usage: TokenUsage, *, account_id: str | None = None) -> None:
        """Record the real usage of a finished call.

        ``usage.total`` counts prompt, cached prompt and completion tokens, so the
        counters match the policy caps exactly (HARD RULE 3: budgets use ``total``).
        """
        total = usage.total
        self._run_used += total
        self._iteration_used += total
        if account_id is not None:
            self._account_usage[account_id] = self._account_usage.get(account_id, 0) + total

    def reset(self) -> None:
        """Forget every counter. Used between runs and in tests."""
        self._run_used = 0
        self._iteration_used = 0
        self._iteration = 0
        self._account_usage.clear()


#: Names of the caps the governor can refuse a call for. Stable, machine-readable.
BUDGET_CAPS: Final[tuple[str, ...]] = (
    "call_input_cap",
    "binding_max_input_tokens",
    "binding_max_output_tokens",
    "iteration_token_cap",
    "run_token_cap",
    "task_token_budget",
)
