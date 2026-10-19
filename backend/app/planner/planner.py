"""The Planner: produces :class:`Plan` and :class:`Verdict`, nothing else.

Planning-only by construction (ADR-0001): the public surface is ``plan_initial``,
``replan`` and ``evaluate``; there is no method that can return article text. Every
call goes through the same structured pipeline:

1. refuse an over-budget context before spending anything (digest cap, then
   ``call_input_cap``, then the optional governor :class:`BudgetGuard`);
2. one provider call under ``BindingRole.PLANNER`` with the contract JSON schema
   and a hard ``max_output_tokens`` - accounts resolve at call time because the
   provider is asked per call, never cached with a key (see
   :class:`PoolBackedProvider` for the production wiring);
3. parse + semantically validate (see :mod:`app.planner.validation`); on failure,
   spend at most ``max_repairs`` context-free repair calls listing *every* issue;
4. for verdicts, apply the deterministic report guardrails.

Tokens of failed attempts are spent tokens: outcomes carry the summed usage of
every call made, so the governor and ledger can bill repairs honestly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar

from app.contracts.enums import AgentRole, BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.plan import REQUIRED_ROLES, Plan
from app.contracts.verdict import Verdict
from app.core.ids import UlidFactory
from app.core.policies import DEFAULT_POLICIES, Policies
from app.memory.ledger import sum_token_usage
from app.planner.context import PlannerContext
from app.planner.errors import PlannerContextTooLarge, PlannerError, PlannerOutputError
from app.planner.prompts import (
    build_plan_request,
    build_repair_request,
    build_verdict_request,
    payload_text,
    request_parts,
)
from app.planner.validation import apply_report_guardrails, parse_plan, parse_verdict
from app.providers.base import LLMProvider, estimate_tokens, prompt_tokens
from app.providers.errors import ProviderBadResponse

__all__ = [
    "BudgetGuard",
    "PlanOutcome",
    "Planner",
    "PlannerConfig",
    "PlannerPoolLike",
    "PoolBackedProvider",
    "VerdictOutcome",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BudgetGuard(Protocol):
    """Optional pre-call budget seam (the governor implements this in its step).

    ``precheck`` raising ``BudgetExceeded`` aborts the call before any provider is
    touched. Recording stays with the caller via ``outcome.usage``: the pool
    already bills accounts per call, and double-recording was a real bug class in
    the governor step's review.
    """

    def precheck(self, request: LLMRequest, *, role: str) -> None:
        """Raise if ``request`` may not be spent by ``role`` right now."""
        ...


class PlannerPoolLike(Protocol):
    """The slice of :class:`~app.providers.pool.ProviderPool` the adapter needs."""

    async def generate(
        self,
        request: LLMRequest,
        *,
        role: str,
        user_id: str,
        model: str | None = None,
    ) -> LLMResponse:
        """One call with accounts resolved at call time for ``role``."""
        ...

    async def aclose(self) -> None:
        """Release pooled providers."""
        ...


class PoolBackedProvider:
    """Adapt a ``ProviderPool`` to the provider the Planner (and tests) speak to.

    The pool owns binding/account resolution per call, so a key rotated or revoked
    mid-run takes effect on the next Planner call without rebuilding anything.
    """

    name: ClassVar[str] = "pool"

    def __init__(self, pool: PlannerPoolLike, *, user_id: str) -> None:
        self._pool = pool
        self._user_id = user_id

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        """Delegate to the pool; ``account_id`` is the pool's business, not ours."""
        _ = account_id
        return await self._pool.generate(
            request,
            role=role or BindingRole.PLANNER.value,
            user_id=self._user_id,
            model=model,
        )

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Shared ``chars / 4`` estimate; the pool exposes no vendor tokeniser."""
        return prompt_tokens(request)

    async def aclose(self) -> None:
        """Close the underlying pool."""
        await self._pool.aclose()


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Planner knobs not owned by ``config/policies.yaml`` (per-call output caps).

    ``initial_required_roles`` is the contracts' ``REQUIRED_ROLES``: an initial
    plan must schedule architecture, research, writing and validation. A replan
    only schedules the next iteration, so it must still end in validation but may
    legitimately be e.g. writer + validator only.
    """

    plan_max_output_tokens: int = 4_096
    verdict_max_output_tokens: int = 1_024
    max_repairs: int = 1
    max_source_refs: int = 100
    initial_required_roles: frozenset[AgentRole] = field(default=REQUIRED_ROLES)
    replan_required_roles: frozenset[AgentRole] = field(default=frozenset({AgentRole.VALIDATOR}))

    def __post_init__(self) -> None:
        if not 64 <= self.plan_max_output_tokens <= 64_000:
            raise PlannerError("plan_max_output_tokens must be within 64..64000")
        if not 64 <= self.verdict_max_output_tokens <= 64_000:
            raise PlannerError("verdict_max_output_tokens must be within 64..64000")
        if not 0 <= self.max_repairs <= 3:
            raise PlannerError("max_repairs must be within 0..3 (repairs spend real tokens)")
        if self.max_source_refs < 1:
            raise PlannerError("max_source_refs must be >= 1")
        if not self.initial_required_roles:
            raise PlannerError("initial_required_roles must not be empty")


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """A validated plan plus the cost of obtaining it."""

    plan: Plan
    usage: TokenUsage
    attempts: int
    model: str
    repaired_issues: tuple[str, ...] = ()

    @property
    def repairs_used(self) -> int:
        """Repair calls spent (``attempts - 1``)."""
        return self.attempts - 1


@dataclass(frozen=True, slots=True)
class VerdictOutcome:
    """A guardrail-checked verdict plus cost and the adjustments applied."""

    verdict: Verdict
    usage: TokenUsage
    attempts: int
    model: str
    repaired_issues: tuple[str, ...] = ()
    guardrail_adjustments: tuple[str, ...] = ()

    @property
    def repairs_used(self) -> int:
        """Repair calls spent (``attempts - 1``)."""
        return self.attempts - 1


@dataclass(frozen=True, slots=True)
class _StructuredOutcome(Generic[T]):
    value: T
    usage: TokenUsage
    attempts: int
    model: str
    repaired_issues: tuple[str, ...]


class Planner:
    """LLM-backed Planner over any ``LLMProvider`` (MockProvider in tests - HARD RULE 5)."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        policies: Policies = DEFAULT_POLICIES,
        config: PlannerConfig | None = None,
        id_factory: Callable[[], str] | None = None,
        budget_guard: BudgetGuard | None = None,
        model: str | None = None,
        account_id: str | None = None,
    ) -> None:
        self._provider = provider
        self._policies = policies
        self._config = config or PlannerConfig()
        self._id_factory: Callable[[], str] = id_factory or UlidFactory()
        self._budget_guard = budget_guard
        self._model = model
        self._account_id = account_id

    # -- public API ----------------------------------------------------------

    async def plan_initial(self, context: PlannerContext) -> PlanOutcome:
        """Plan version 1, covering every required role, budgeted against the run cap."""
        budget_cap = (
            context.token_budget_cap
            if context.token_budget_cap is not None
            else self._policies.run_token_cap
        )
        return await self._plan(
            context,
            mode="initial",
            expected_version=1,
            require_roles=self._config.initial_required_roles,
            budget_cap=budget_cap,
        )

    async def replan(self, context: PlannerContext) -> PlanOutcome:
        """Next plan version from the digest; schedules one iteration, ends in validation."""
        if context.previous_plan is None:
            raise PlannerError("replan requires context.previous_plan (use plan_initial first)")
        budget_cap = (
            context.token_budget_cap
            if context.token_budget_cap is not None
            else self._policies.iteration_token_cap
        )
        return await self._plan(
            context,
            mode="replan",
            expected_version=context.previous_plan.version + 1,
            require_roles=self._config.replan_required_roles,
            budget_cap=budget_cap,
        )

    async def evaluate(self, context: PlannerContext) -> VerdictOutcome:
        """Evaluate the current state; the report guardrails run before returning."""
        self._enforce_digest_cap(context)
        request = build_verdict_request(
            context,
            q_min=self._policies.q_min,
            max_output_tokens=self._config.verdict_max_output_tokens,
            max_source_refs=self._config.max_source_refs,
        )

        def _parse(text: str) -> tuple[Verdict | None, tuple[str, ...], dict[str, Any] | None]:
            result = parse_verdict(text)
            return result.verdict, result.issues, result.payload

        outcome = await self._structured(
            contract_name="Verdict",
            request=request,
            max_output_tokens=self._config.verdict_max_output_tokens,
            parse=_parse,
        )
        guarded = apply_report_guardrails(outcome.value, context.report, q_min=self._policies.q_min)
        if guarded.adjustments:
            logger.warning("verdict guardrails applied: %s", ", ".join(guarded.adjustments))
        return VerdictOutcome(
            verdict=guarded.verdict,
            usage=outcome.usage,
            attempts=outcome.attempts,
            model=outcome.model,
            repaired_issues=outcome.repaired_issues,
            guardrail_adjustments=guarded.adjustments,
        )

    # -- pipeline ------------------------------------------------------------

    async def _plan(
        self,
        context: PlannerContext,
        *,
        mode: Literal["initial", "replan"],
        expected_version: int,
        require_roles: frozenset[AgentRole],
        budget_cap: int,
    ) -> PlanOutcome:
        self._enforce_digest_cap(context)
        request = build_plan_request(
            context,
            mode=mode,
            expected_version=expected_version,
            require_roles=require_roles,
            budget_cap=budget_cap,
            max_output_tokens=self._config.plan_max_output_tokens,
            max_source_refs=self._config.max_source_refs,
        )
        known_sections = context.known_section_ids()
        known_sources = context.known_source_ids()

        def _parse(text: str) -> tuple[Plan | None, tuple[str, ...], dict[str, Any] | None]:
            result = parse_plan(
                text,
                expected_version=expected_version,
                id_factory=self._id_factory,
                known_section_ids=known_sections,
                known_source_ids=known_sources,
                require_roles=require_roles,
                budget_cap=budget_cap,
            )
            return result.plan, result.issues, result.payload

        outcome = await self._structured(
            contract_name="Plan",
            request=request,
            max_output_tokens=self._config.plan_max_output_tokens,
            parse=_parse,
        )
        return PlanOutcome(
            plan=outcome.value,
            usage=outcome.usage,
            attempts=outcome.attempts,
            model=outcome.model,
            repaired_issues=outcome.repaired_issues,
        )

    async def _structured(
        self,
        *,
        contract_name: Literal["Plan", "Verdict"],
        request: LLMRequest,
        max_output_tokens: int,
        parse: Callable[[str], tuple[T | None, tuple[str, ...], dict[str, Any] | None]],
    ) -> _StructuredOutcome[T]:
        """Call, parse and boundedly repair one structured Planner output."""
        usages: list[TokenUsage] = []
        repaired_issues: list[str] = []
        current = request
        last_text = ""
        last_issues: tuple[str, ...] = ("no provider reply received",)
        model = ""
        attempts = 0
        for attempt in range(self._config.max_repairs + 1):
            attempts = attempt + 1
            self._enforce_input_cap(current)
            if self._budget_guard is not None:
                self._budget_guard.precheck(current, role=BindingRole.PLANNER.value)
            try:
                response = await self._provider.generate(
                    current,
                    model=self._model,
                    role=BindingRole.PLANNER.value,
                    account_id=self._account_id,
                )
            except ProviderBadResponse as exc:
                # A vendor can surface garbage output as an error instead of text;
                # either way it is one repairable shape failure, not a crash.
                last_text = str(exc.context.get("body_preview") or "")
                last_issues = (f"provider returned an unusable response ({exc.code})",)
                if attempt >= self._config.max_repairs:
                    raise PlannerOutputError(
                        f"Planner {contract_name} output unusable after {attempts} attempt(s)",
                        issues=last_issues,
                        attempts=attempts,
                        body_preview=last_text or None,
                    ) from exc
                repaired_issues.extend(last_issues)
                current = build_repair_request(
                    contract_name=contract_name,
                    previous_text=last_text or "(no response body)",
                    issues=last_issues,
                    max_output_tokens=max_output_tokens,
                )
                continue

            usages.append(response.usage)
            model = response.model
            last_text = response.text
            value, issues, payload = parse(response.text)
            if value is not None and not issues:
                return _StructuredOutcome(
                    value=value,
                    usage=sum_token_usage(usages),
                    attempts=attempts,
                    model=model,
                    repaired_issues=tuple(repaired_issues),
                )
            last_issues = issues or ("reply parsed to an empty result",)
            if attempt >= self._config.max_repairs:
                break
            logger.warning(
                "planner %s attempt %d invalid (%d issue(s)); repairing",
                contract_name,
                attempts,
                len(last_issues),
            )
            repaired_issues.extend(last_issues)
            current = build_repair_request(
                contract_name=contract_name,
                previous_text=payload_text(payload, response.text),
                issues=last_issues,
                max_output_tokens=max_output_tokens,
            )

        raise PlannerOutputError(
            f"Planner {contract_name} output invalid after {attempts} attempt(s)",
            issues=last_issues,
            attempts=attempts,
            body_preview=last_text or None,
        )

    # -- caps ------------------------------------------------------------------

    def _enforce_digest_cap(self, context: PlannerContext) -> None:
        """Refuse history above ``digest_max_tokens`` (ADR-0002: bounded or nothing)."""
        if not context.digest_markdown:
            return
        tokens = estimate_tokens(context.digest_markdown)
        if tokens > self._policies.digest_max_tokens:
            raise PlannerContextTooLarge(
                f"digest is {tokens} tokens, above digest_max_tokens "
                f"{self._policies.digest_max_tokens}; compact before planning",
                tokens=tokens,
                cap=self._policies.digest_max_tokens,
                parts={"digest": tokens},
            )

    def _enforce_input_cap(self, request: LLMRequest) -> None:
        """Refuse (never truncate) a prompt above ``call_input_cap``."""
        tokens = prompt_tokens(request)
        if tokens > self._policies.call_input_cap:
            raise PlannerContextTooLarge(
                f"planner prompt is {tokens} tokens, above call_input_cap "
                f"{self._policies.call_input_cap}",
                tokens=tokens,
                cap=self._policies.call_input_cap,
                parts=request_parts(request),
            )
