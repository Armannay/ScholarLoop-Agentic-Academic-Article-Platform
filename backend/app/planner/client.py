"""LLMPlanner - the planning-only Planner as a structured-output client.

One Planner call is one JSON-schema provider call against the frozen
``LLMProvider`` boundary (or the account-resolving provider pool, which has
the same shape plus a ``user_id``). The client owns three things and nothing
else:

* **the guardrails** - the provider must be Anthropic or the mock (Claude
  Console key, never a subscription login), the digest must already be within
  its token bound, and the rendered request must fit ``call_input_cap``
  (refuse, never truncate);
* **the parse + one repair** - replies are validated against the contracts;
  a bad reply earns exactly one bounded repair retry carrying only the
  machine-readable rejection summary, then :class:`PlannerOutputError`;
* **the bill** - every response's usage is accumulated into
  :attr:`PlannerPort.tokens_spent` (repair attempts included), because planner
  calls are not ``ChangeEntry`` rows and the orchestrator still has to count
  them against the run budget.

Plan-level rules that the ``Plan`` contract does not itself enforce are
checked here so a repair retry can fix them: the dictated ``version``, the
mandatory role distribution, known section ids only, instructions within the
word bound, and task budgets summing to at most the iteration cap.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.contracts.brief import Brief
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.plan import REQUIRED_ROLES, Plan
from app.contracts.quality import QualityReport
from app.contracts.task import MAX_TASK_INSTRUCTIONS_WORDS
from app.contracts.verdict import Verdict
from app.core.errors import BudgetExceeded, format_validation_errors
from app.core.policies import DEFAULT_POLICIES, Policies
from app.core.rubric import SectionSpec
from app.planner.errors import PlannerError, PlannerOutputError, PlannerProviderNotAllowed
from app.planner.prompts import (
    DEFAULT_PLANNER_MAX_OUTPUT_TOKENS,
    PLANNER_ROLE,
    build_evaluate_request,
    build_plan_request,
)
from app.providers.base import estimate_tokens, prompt_tokens
from app.providers.errors import ProviderBadResponse

__all__ = [
    "ALLOWED_PLANNER_PROVIDERS",
    "LLMPlanner",
    "PlannerPool",
    "PlannerPort",
    "PlannerProvider",
]

logger = logging.getLogger(__name__)

#: Provider names the planner may run on. The mock keeps tests honest (HARD RULE 5);
#: production is Anthropic only - enforced again here, at the point of use.
ALLOWED_PLANNER_PROVIDERS: frozenset[str] = frozenset({"anthropic", "mock"})

_MAX_REPAIR_HINT_CHARS = 320

T = TypeVar("T", bound=BaseModel)


class PlannerProvider(Protocol):
    """The direct provider shape (what :class:`LLMProvider` implementations offer)."""

    name: ClassVar[str]

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> LLMResponse:
        """Execute one call. Raises only :class:`ProviderError`."""
        ...


class PlannerPool(Protocol):
    """The pool shape: like a provider, but it resolves accounts for a user."""

    async def generate(
        self,
        request: LLMRequest,
        *,
        role: str,
        user_id: str,
        model: str | None = None,
    ) -> LLMResponse:
        """Execute one call through ``role``'s binding for ``user_id``."""
        ...


class PlannerPort(Protocol):
    """What the orchestrator needs from a planner - LLM-backed or scripted."""

    @property
    def tokens_spent(self) -> int:
        """Total tokens billed to this planner so far (run-budget accounting)."""
        ...

    async def plan(
        self,
        brief: Brief,
        *,
        digest_text: str = "",
        sections: Sequence[SectionSpec] = (),
        version: int = 1,
    ) -> Plan:
        """Produce the plan for ``brief`` at the dictated ``version``."""
        ...

    async def replan(
        self,
        brief: Brief,
        *,
        digest_text: str,
        sections: Sequence[SectionSpec] = (),
        version: int,
    ) -> Plan:
        """Produce the next plan version from the digest of what already happened."""
        ...

    async def evaluate(
        self,
        brief: Brief,
        report: QualityReport,
        *,
        digest_text: str = "",
    ) -> Verdict:
        """Evaluate a quality report into a (purely advisory) verdict."""
        ...


class LLMPlanner:
    """A :class:`PlannerPort` backed by one provider or by the provider pool.

    Exactly one of ``provider`` / ``pool`` must be supplied. With a pool, the
    planner's Anthropic-only rule is enforced by the account store's binding
    validation; with a direct provider it is enforced here by provider name.
    """

    def __init__(
        self,
        provider: PlannerProvider | None = None,
        *,
        pool: PlannerPool | None = None,
        user_id: str | None = None,
        model: str | None = None,
        account_id: str | None = None,
        policies: Policies = DEFAULT_POLICIES,
        max_output_tokens: int = DEFAULT_PLANNER_MAX_OUTPUT_TOKENS,
        max_repairs: int = 1,
        rules_text: str | None = None,
    ) -> None:
        if (provider is None) == (pool is None):
            raise PlannerError("LLMPlanner needs exactly one of provider= or pool=")
        if pool is not None and not user_id:
            raise PlannerError("a pooled planner needs the user_id whose binding resolves")
        if provider is not None and provider.name not in ALLOWED_PLANNER_PROVIDERS:
            raise PlannerProviderNotAllowed(
                f"the planner must run on anthropic (Claude Console key) or mock, "
                f"not {provider.name!r}",
                provider=provider.name,
            )
        if max_output_tokens < 64:
            raise PlannerError("max_output_tokens for the planner must be >= 64")
        if max_repairs not in (0, 1):
            raise PlannerError("max_repairs is 0 or 1: a planner gets one repair, not a loop")
        self._provider = provider
        self._pool = pool
        self._user_id = user_id
        self._model = model
        self._account_id = account_id
        self._policies = policies
        self._max_output_tokens = max_output_tokens
        self._max_repairs = max_repairs
        self._rules_text = rules_text
        self._tokens_spent = 0
        self._calls = 0

    # -- accounting ---------------------------------------------------------

    @property
    def tokens_spent(self) -> int:
        """Summed ``TokenUsage.total`` over every planner response so far."""
        return self._tokens_spent

    @property
    def call_count(self) -> int:
        """Provider calls attempted, repair retries included."""
        return self._calls

    # -- PlannerPort ----------------------------------------------------------

    async def plan(
        self,
        brief: Brief,
        *,
        digest_text: str = "",
        sections: Sequence[SectionSpec] = (),
        version: int = 1,
    ) -> Plan:
        """Produce the plan for ``brief`` at the dictated ``version``."""
        self._check_digest(digest_text)
        known_sections = tuple(section.id for section in sections)

        def build(hint: str | None) -> LLMRequest:
            return build_plan_request(
                brief,
                digest_text=digest_text,
                sections=sections,
                version=version,
                policies=self._policies,
                max_output_tokens=self._max_output_tokens,
                rules_text=self._rules_text,
                repair_hint=hint,
            )

        def validate(candidate: Plan) -> None:
            self._validate_plan(candidate, version=version, known_sections=known_sections)

        return await self._structured(build, Plan, validate)

    async def replan(
        self,
        brief: Brief,
        *,
        digest_text: str,
        sections: Sequence[SectionSpec] = (),
        version: int,
    ) -> Plan:
        """Replan from the digest: a plan call whose history is non-empty."""
        return await self.plan(brief, digest_text=digest_text, sections=sections, version=version)

    async def evaluate(
        self,
        brief: Brief,
        report: QualityReport,
        *,
        digest_text: str = "",
    ) -> Verdict:
        """Evaluate ``report`` into a ``Verdict`` (advice for the loop controller)."""
        self._check_digest(digest_text)

        def build(hint: str | None) -> LLMRequest:
            return build_evaluate_request(
                brief,
                report,
                digest_text=digest_text,
                max_output_tokens=self._max_output_tokens,
                rules_text=self._rules_text,
                repair_hint=hint,
            )

        return await self._structured(build, Verdict, None)

    # -- internals ------------------------------------------------------------

    def _check_digest(self, digest_text: str) -> None:
        """Refuse an over-bound digest instead of truncating someone's history."""
        tokens = estimate_tokens(digest_text)
        if tokens > self._policies.digest_max_tokens:
            raise PlannerError(
                f"digest is {tokens} tokens, over the {self._policies.digest_max_tokens} "
                "token bound; rebuild it through the digest builder instead of "
                "passing raw history to the planner",
                digest_tokens=tokens,
                digest_max_tokens=self._policies.digest_max_tokens,
            )

    def _validate_plan(self, plan: Plan, *, version: int, known_sections: tuple[str, ...]) -> None:
        """Plan rules beyond the contract, phrased so a repair retry can act on them."""
        if plan.version != version:
            raise PlannerOutputError(
                f"plan.version is {plan.version}, the orchestrator dictated {version}"
            )
        # NOTE: Plan.missing_required_roles() diffs against the keys of a
        # zero-filled role_distribution(), so it always returns [] today (see
        # the CONTRACT-CHANGE-REQUEST in this step's handoff). The planner
        # therefore counts roles itself; the mandatory distribution is too
        # load-bearing to inherit that answer.
        present = {role for role, count in plan.role_distribution().items() if count > 0}
        missing = sorted(REQUIRED_ROLES - present, key=lambda role: role.value)
        if missing:
            names = ", ".join(role.value for role in missing)
            raise PlannerOutputError(f"plan has no task for required role(s): {names}")
        total_budget = sum(task.token_budget for task in plan.tasks)
        if total_budget > self._policies.iteration_token_cap:
            raise PlannerOutputError(
                f"task token_budgets sum to {total_budget}, over the iteration "
                f"cap {self._policies.iteration_token_cap}"
            )
        if known_sections:
            known = set(known_sections)
            for task in plan.tasks:
                invented = sorted(set(task.section_ids) - known)
                if invented:
                    raise PlannerOutputError(
                        f"task '{task.title}' names unknown section(s) {invented}; "
                        "use only the rubric section ids given"
                    )
        for task in plan.tasks:
            words = task.instruction_word_count()
            if words > MAX_TASK_INSTRUCTIONS_WORDS:
                raise PlannerOutputError(
                    f"task '{task.title}' instructions are {words} words, over the "
                    f"{MAX_TASK_INSTRUCTIONS_WORDS} word bound"
                )

    async def _structured(
        self,
        build: Callable[[str | None], LLMRequest],
        model: type[T],
        validate: Callable[[T], None] | None,
    ) -> T:
        """One structured call plus at most one repair retry on bad output only."""
        hint: str | None = None
        last_error: PlannerOutputError | None = None
        for _attempt in range(self._max_repairs + 1):
            request = build(hint)
            self._check_input_cap(request)
            try:
                response = await self._generate(request)
            except ProviderBadResponse as exc:
                # A malformed-JSON failure is bad output by another path: repairable.
                # Every other ProviderError (timeout, rate limit, auth, no account)
                # propagates - retry/failover policy belongs to the pool and the loop.
                last_error = PlannerOutputError(
                    f"provider returned an unusable response: {exc.message}",
                    provider=exc.provider,
                )
                hint = "the previous reply was not valid JSON for the schema"
                continue
            # Billed whether or not the text parses: count it before validating.
            self._tokens_spent += response.usage.total
            try:
                parsed = self._parse(response.text, model)
                if validate is not None:
                    validate(parsed)
            except PlannerOutputError as exc:
                last_error = exc
                hint = _repair_hint(exc)
                logger.info("planner output rejected, repairing once: %s", hint)
                continue
            return parsed
        assert last_error is not None  # the loop only falls through after a rejection
        raise PlannerOutputError(
            f"planner output still invalid after {self._max_repairs} repair "
            f"attempt(s): {last_error.message}",
            attempts=self._max_repairs + 1,
        ) from last_error

    def _check_input_cap(self, request: LLMRequest) -> None:
        """The governor's seatbelt until it lands: refuse, never truncate."""
        estimated = prompt_tokens(request)
        if estimated > self._policies.call_input_cap:
            raise BudgetExceeded(
                f"planner prompt is {estimated} tokens, over call_input_cap "
                f"{self._policies.call_input_cap}",
                cap="call_input_cap",
                limit=self._policies.call_input_cap,
                estimated=estimated,
            )

    async def _generate(self, request: LLMRequest) -> LLMResponse:
        """Dispatch one call to the direct provider or the pool."""
        self._calls += 1
        if self._pool is not None:
            assert self._user_id is not None  # enforced in __init__
            return await self._pool.generate(
                request, role=PLANNER_ROLE, user_id=self._user_id, model=self._model
            )
        assert self._provider is not None  # enforced in __init__
        return await self._provider.generate(
            request, model=self._model, role=PLANNER_ROLE, account_id=self._account_id
        )

    @staticmethod
    def _parse(text: str, model: type[T]) -> T:
        """Parse a reply into its contract, rejecting anything that is not clean JSON."""
        candidate = text.strip()
        if candidate.startswith("```"):
            # Models under a JSON schema should not fence, but a fenced reply is
            # still unambiguous data - unlike, say, JSON embedded in prose.
            candidate = candidate.strip("`")
            if candidate.startswith("json"):
                candidate = candidate[len("json") :].strip()
        try:
            payload: Any = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise PlannerOutputError(
                f"reply is not valid JSON: {exc.msg} at line {exc.lineno}"
            ) from exc
        if not isinstance(payload, dict):
            raise PlannerOutputError("reply JSON must be an object")
        if payload.get("mock") is True and "version" not in payload and "tasks" not in payload:
            raise PlannerOutputError("unscripted MockProvider stub carries no contract body")
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise PlannerOutputError(
                f"reply failed {model.__name__} validation: {format_validation_errors(exc)}"
            ) from exc


def _repair_hint(error: PlannerOutputError) -> str:
    """A bounded, single-line rejection summary for the repair request.

    Only our own validation text travels back into the prompt - never the
    model's rejected reply, which is untrusted (HARD RULE 4) and expensive.
    """
    return " ".join(error.message.split())[:_MAX_REPAIR_HINT_CHARS]
