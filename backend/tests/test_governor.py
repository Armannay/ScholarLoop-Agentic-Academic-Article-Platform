"""The token governor: refuse over-budget calls, never truncate them.

The governor is pure arithmetic, so every test here states exact numbers. The
property that matters most is the *refusal*: a call that does not fit is an
error with a machine-readable cap name, not a silently shortened prompt.
"""

from __future__ import annotations

import pytest

from app.contracts.binding import AgentBinding
from app.contracts.enums import BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest
from app.core.errors import BudgetExceeded
from app.core.policies import Policies
from app.governor.budget import BUDGET_CAPS, TokenGovernor

pytestmark = pytest.mark.providers


def make_request(
    text: str = "Draft the introduction.", *, max_output_tokens: int = 1_000
) -> LLMRequest:
    """A request with a predictable prompt size (4 characters per token)."""
    return LLMRequest(
        system_blocks=[{"text": "You are the writer agent."}],
        messages=[{"role": "user", "text": text}],
        max_output_tokens=max_output_tokens,
    )


def make_binding(**overrides: object) -> AgentBinding:
    """A binding with generous ceilings; tests tighten them one at a time."""
    from tests.factories import sample_ulid

    values: dict[str, object] = {
        "role": BindingRole.WRITER,
        "account_ids": [sample_ulid(0)],
        "strategy": "dedicated",
        "model": "test-model",
        "max_input_tokens": 24_000,
        "max_output_tokens": 8_000,
    }
    values.update(overrides)
    return AgentBinding.model_validate(values)


@pytest.fixture
def policies() -> Policies:
    """The shipped policies, with the run cap lowered so the numbers stay small."""
    return Policies(run_token_cap=20_000, iteration_token_cap=8_000, call_input_cap=4_000)


@pytest.fixture
def governor(policies: Policies) -> TokenGovernor:
    """A governor that enforces ``policies``."""
    return TokenGovernor(policies)


# --------------------------------------------------------------------------- happy path
def test_a_call_inside_every_cap_is_allowed(governor: TokenGovernor) -> None:
    """A normal call passes and reports what it will cost."""
    request = make_request()
    precheck = governor.precheck(request, make_binding())
    assert precheck.prompt_tokens > 0
    assert precheck.estimated_total == precheck.prompt_tokens + request.max_output_tokens
    assert precheck.model == "test-model"


def test_charge_moves_both_counters(governor: TokenGovernor) -> None:
    """Charged tokens count towards the run and towards the iteration."""
    governor.charge(TokenUsage(in_=1_000, out=200, cached=300))
    assert governor.run_used == 1_500
    assert governor.iteration_used == 1_500
    assert governor.account_used("acc-1") == 0
    governor.charge(TokenUsage(in_=10, out=0, cached=0), account_id="acc-1")
    assert governor.account_used("acc-1") == 10


def test_a_new_iteration_restarts_the_iteration_counter(governor: TokenGovernor) -> None:
    """Iterations are budgeted separately; the run keeps counting."""
    governor.charge(TokenUsage(in_=1_000, out=0, cached=0))
    governor.begin_iteration(2)
    assert governor.iteration == 2
    assert governor.iteration_used == 0
    assert governor.run_used == 1_000


def test_the_remaining_budget_shrinks(governor: TokenGovernor) -> None:
    """The governor can say how much is left."""
    assert governor.run_remaining == 20_000
    governor.charge(TokenUsage(in_=1_000, out=0, cached=0))
    assert governor.run_remaining == 19_000
    assert governor.iteration_remaining == 7_000


# --------------------------------------------------------------------------- refusals
def test_a_prompt_above_the_call_input_cap_is_refused(governor: TokenGovernor) -> None:
    """The per-call input cap is the first thing the governor checks."""
    request = make_request("x" * 20_000)
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(request, make_binding())
    assert excinfo.value.context["cap"] == "call_input_cap"


def test_a_prompt_above_the_binding_ceiling_is_refused(governor: TokenGovernor) -> None:
    """A binding may be tighter than the global cap."""
    request = make_request("x" * 5_000)
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(request, make_binding(max_input_tokens=1_000, max_output_tokens=500))
    assert excinfo.value.context["cap"] == "binding_max_input_tokens"


def test_an_output_above_the_binding_ceiling_is_refused(governor: TokenGovernor) -> None:
    """A request may not ask for more output than the binding allows."""
    request = make_request(max_output_tokens=9_000)
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(request, make_binding(max_output_tokens=8_000))
    assert excinfo.value.context["cap"] == "binding_max_output_tokens"


def test_a_call_beyond_the_iteration_cap_is_refused(governor: TokenGovernor) -> None:
    """The iteration cap is enforced against the tokens already spent."""
    governor.charge(TokenUsage(in_=7_500, out=0, cached=0))
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(make_request(), make_binding())
    assert excinfo.value.context["cap"] == "iteration_token_cap"


def test_a_call_beyond_the_run_cap_is_refused(governor: TokenGovernor) -> None:
    """The run cap is the last word, even when the iteration has room.

    The spend is spread over three iterations so that the *iteration* cap is not
    the one that trips: only the run total is over.
    """
    for index, tokens in enumerate((7_000, 7_000, 6_000), start=1):
        governor.begin_iteration(index)
        governor.charge(TokenUsage(in_=tokens, out=0, cached=0))

    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(make_request(), make_binding())
    assert excinfo.value.context["cap"] == "run_token_cap"


def test_a_call_beyond_the_task_budget_is_refused(governor: TokenGovernor) -> None:
    """A task budget carried by the run state is honoured as well."""
    run_state = {"task_token_budget": 2_000, "task_tokens_used": 1_500}
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(make_request(max_output_tokens=1_000), make_binding(), run_state)
    assert excinfo.value.context["cap"] == "task_token_budget"


def test_the_run_state_counters_are_read_when_present(governor: TokenGovernor) -> None:
    """A caller that tracks the spend elsewhere is still bounded by the governor."""
    run_state = {"run_tokens_used": 19_500, "iteration_tokens_used": 0}
    with pytest.raises(BudgetExceeded) as excinfo:
        governor.precheck(make_request(), make_binding(), run_state)
    assert excinfo.value.context["cap"] == "run_token_cap"


def test_the_cap_names_are_stable(governor: TokenGovernor) -> None:
    """The names a caller may branch on are part of the contract of this module."""
    assert "run_token_cap" in BUDGET_CAPS
    assert "call_input_cap" in BUDGET_CAPS
    assert "task_token_budget" in BUDGET_CAPS


def test_overspending_is_reported_not_hidden(governor: TokenGovernor) -> None:
    """A call that overshoots the cap is recorded, so the loop can stop."""
    governor.charge(TokenUsage(in_=25_000, out=0, cached=0))
    assert governor.over_budget is True
    assert governor.run_remaining == 0


def test_reset_clears_the_counters(governor: TokenGovernor) -> None:
    """A new run starts from zero."""
    governor.charge(TokenUsage(in_=1_000, out=0, cached=0))
    governor.reset()
    assert governor.run_used == 0
    assert governor.iteration_used == 0
