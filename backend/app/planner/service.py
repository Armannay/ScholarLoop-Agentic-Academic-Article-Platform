"""The Planner service: ``plan``, ``replan`` and ``evaluate`` over existing seams.

The Planner is a *planner and evaluator only*. It returns a ``Plan``, a ``PlanDelta`` or
a ``Verdict``; it never writes article text, never produces patches and never touches the
article store. Every capability it uses is borrowed from a prerequisite step:

* prompt layout, trimming and the mandatory-rule guard - the governor
  :class:`~app.governor.context_builder.ContextBuilder` (see
  :mod:`app.planner.context` for the thin Task-shaped adapter);
* admission control - the injected :class:`~app.agents.base.Governor` (the Step 2
  ``BudgetGovernor`` satisfies it), called with ``role=BindingRole.PLANNER`` and the
  synthetic task, so a planner call is bounded exactly like an agent call;
* accounts - the ``planner`` :class:`~app.contracts.binding.AgentBinding`, resolved at
  *call* time, so an account the user disabled mid-run is never used;
* JSON handling - ``parse_agent_json`` from the agent runtime, tolerating a fence but
  never inventing content.

Retries are capped at :data:`MAX_PLANNER_RETRIES` (2) and each retry carries the
validator's error list only: no previous plan, no previous evaluation, no article text.

Concurrency note: the service is safe to share across tasks (no instance state is read
across an ``await`` boundary except immutable copies), but the diagnostic properties
(``usage``, ``last_prompt``) describe the most recent call, not the whole run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final, TypeVar

from pydantic import BaseModel, ValidationError

from app.agents.base import AgentProvider, BindingResolver, Governor, parse_agent_json
from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.llm import LLMResponse
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.errors import BudgetExceeded, ContractViolation, format_validation_errors
from app.core.hashing import content_hash
from app.core.ids import new_ulid
from app.core.policies import DEFAULT_POLICIES, Policies
from app.core.rubric import Rubric
from app.memory.digest import DigestSnapshot
from app.planner.budget import PlanBudget
from app.planner.context import (
    EVALUATE,
    PLAN,
    REPLAN,
    PlannerContexts,
    PlannerPromptBuilder,
    PlannerRequest,
    PlannerRunState,
    planner_task,
    retry_instruction,
)
from app.planner.drafts import (
    EvaluationDraft,
    PlanDeltaDraft,
    PlanDraft,
    evaluation_schema,
    plan_delta_schema,
    plan_draft_schema,
)
from app.planner.errors import (
    EvaluationRejected,
    PlannerOutputInvalid,
    PlannerRejected,
    PlannerStateMissing,
    PlannerUnavailable,
    PlanRejected,
)
from app.planner.evaluation import Evaluation, evaluation_problems, merge_verdict, weighted_q
from app.planner.plan_validator import PlanDelta, PlanValidator
from app.planner.probes import ProbeReport
from app.planner.prompts import (
    EVALUATOR_MAX_OUTPUT_TOKENS,
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_MAX_OUTPUT_TOKENS,
    PLANNER_SYSTEM_PROMPT,
    ensure_prompt_budget,
)

__all__ = ["MAX_PLANNER_RETRIES", "PlannerService"]

#: The step brief allows at most two retries after the first answer.
MAX_PLANNER_RETRIES: Final[int] = 2
#: How many validator issues travel in an evaluation payload.
MAX_PAYLOAD_ISSUES: Final[int] = 20

ModelT = TypeVar("ModelT", bound=BaseModel)

_ZERO_USAGE: Final[dict[str, int]] = {"in": 0, "out": 0, "cached": 0}


def _usage_add(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Sum two usage records without double counting."""
    return TokenUsage(
        in_=left.in_ + right.in_,
        out=left.out + right.out,
        cached=left.cached + right.cached,
    )


def _digest_text(digest: str | DigestSnapshot | None) -> str:
    """Normalise the digest argument to the markdown slice the builder expects."""
    if digest is None:
        return ""
    if isinstance(digest, str):
        return digest.strip()
    if isinstance(digest, DigestSnapshot):
        return digest.markdown.strip()
    raise TypeError(f"digest must be a str, a DigestSnapshot or None, got {type(digest).__name__}")


class PlannerService:
    """Plans, replans and evaluates against the frozen contracts.

    Args:
        provider: any :class:`~app.agents.base.AgentProvider` (``MockProvider`` in tests,
            a pool adapter in production). Never a vendor SDK.
        bindings: resolved at every call, so account changes are seen immediately.
        governor: the Step 2 governor (or the Step 5 ``CallBudgetGovernor``) precheck.
        contexts: the two :class:`ContextBuilder` instances (planning and evaluation).
        rubric: the loaded rubric - sections for validation, criteria for scoring.
        policies: policy set; ``q_min`` and ``call_input_cap`` are read here.
        brief: the run's brief. :meth:`plan` sets it; :meth:`evaluate` needs it.
        id_factory: source of ULIDs for accepted tasks (deterministic in tests).
        max_retries: retries after the first answer; 0..2.
    """

    def __init__(
        self,
        *,
        provider: AgentProvider,
        bindings: BindingResolver,
        governor: Governor,
        contexts: PlannerContexts,
        rubric: Rubric,
        policies: Policies = DEFAULT_POLICIES,
        brief: Brief | None = None,
        id_factory: Callable[[], str] = new_ulid,
        max_retries: int = MAX_PLANNER_RETRIES,
    ) -> None:
        if not 0 <= max_retries <= MAX_PLANNER_RETRIES:
            raise ValueError(f"max_retries must be 0..{MAX_PLANNER_RETRIES}, got {max_retries}")
        ensure_prompt_budget(PLANNER_SYSTEM_PROMPT, EVALUATOR_SYSTEM_PROMPT)
        self._provider = provider
        self._bindings = bindings
        self._governor = governor
        self._builders = {
            PLAN: PlannerPromptBuilder(contexts.planning),
            REPLAN: PlannerPromptBuilder(contexts.planning),
            EVALUATE: PlannerPromptBuilder(contexts.evaluation),
        }
        self._rubric = rubric
        self._policies = policies
        self._brief = brief
        self._id_factory = id_factory
        self._max_retries = max_retries
        self._validator = PlanValidator(rubric, policies)
        self._plan: Plan | None = None
        self._budget: PlanBudget | None = None
        self._last_evaluation: Evaluation | None = None
        self._deep_fingerprint: str | None = None
        self._usage = TokenUsage(in_=0, out=0, cached=0)
        self._calls = 0
        self._last_prompt: PlannerRequest | None = None

    # -- introspection -----------------------------------------------------
    @property
    def plan_current(self) -> Plan | None:
        """The latest accepted plan, or ``None`` before the first one."""
        return self._plan

    @property
    def last_evaluation(self) -> Evaluation | None:
        """The most recent merged evaluation, with its scores and usage."""
        return self._last_evaluation

    @property
    def last_prompt(self) -> PlannerRequest | None:
        """The most recent assembled request, for telemetry and tests."""
        return self._last_prompt

    @property
    def usage(self) -> TokenUsage:
        """Tokens the planner has spent through this service."""
        return self._usage

    @property
    def calls_made(self) -> int:
        """Provider calls attempted, retries and failures included."""
        return self._calls

    @property
    def brief(self) -> Brief | None:
        """The brief the service is planning and evaluating against."""
        return self._brief

    def set_brief(self, brief: Brief) -> None:
        """Bind the brief without planning (evaluation-only callers need this)."""
        self._brief = brief

    # -- planning ----------------------------------------------------------
    async def plan(
        self,
        brief: Brief,
        digest: str | DigestSnapshot | None,
        budget: PlanBudget | int,
    ) -> Plan:
        """Produce the first plan for ``brief``.

        Args:
            brief: the user's intake document; also becomes the service's brief.
            digest: the bounded ``claude.md`` slice (or its snapshot).
            budget: :class:`PlanBudget`, or a bare remaining-token count.

        Raises:
            PlannerUnavailable: no active ``planner`` binding.
            BudgetExceeded: the governor refused a call.
            ContextTooLarge: the prompt cannot fit the binding's input cap.
            PlannerOutputInvalid: the provider did not answer with a JSON object.
            PlanRejected: the draft was still invalid after the retry budget.
            ProviderError: the provider failed; never converted into a retry.
        """
        resolved_budget = self._coerce_budget(budget)
        self._brief = brief
        self._budget = resolved_budget
        draft, _, _ = await self._ask(
            kind=PLAN,
            role=AgentRole.ARCHITECT,
            instruction=(
                "Produce the plan for this brief. You are the Planner, not an executing "
                "agent: return only the plan JSON."
            ),
            schema=plan_draft_schema(),
            model_type=PlanDraft,
            problems=lambda value: self._validator.problems(
                value, brief=brief, budget=resolved_budget
            ),
            reject=PlanRejected,
            reject_message="the planner could not produce a valid plan",
            budget=resolved_budget,
            digest=digest,
            iteration=resolved_budget.iteration,
        )
        plan = self._validator.build(
            draft,
            brief=brief,
            budget=resolved_budget,
            id_factory=self._id_factory,
            version=1,
        )
        self._plan = plan
        return plan

    async def replan(
        self,
        digest: str | DigestSnapshot | None,
        report: QualityReport | None,
        last_decision: LoopDecision | None,
        strategy_change: bool,
        *,
        plan: Plan | None = None,
        budget: PlanBudget | None = None,
    ) -> PlanDelta:
        """Return the delta that moves the current plan forward.

        Args:
            digest: the bounded ``claude.md`` slice.
            report: the validator's latest report, when there is one.
            last_decision: the loop controller's decision that triggered the replan.
            strategy_change: ``True`` on the first plateau; the prompt then asks for a
                different approach rather than a variation of the same task list.
            plan: override the stored plan (defaults to the last accepted one).
            budget: override the stored budget.

        Returns:
            A validated :class:`~app.planner.plan_validator.PlanDelta`; call
            ``delta.apply_to(plan)`` to obtain the merged plan.
        """
        current = plan if plan is not None else self._plan
        if current is None:
            raise PlannerStateMissing("replan needs a current plan; call plan() first")
        if self._brief is None:
            raise PlannerStateMissing("replan needs the run's brief; call plan() first")
        brief = self._brief
        resolved_budget = (
            budget
            or self._budget
            or PlanBudget.from_policies(self._policies, iteration=current.version)
        )
        payload = self._replan_payload(current, report, last_decision, strategy_change)
        draft, _, _ = await self._ask(
            kind=REPLAN,
            role=AgentRole.ARCHITECT,
            instruction="Revise the plan. Return only the delta JSON: tasks to add, task "
            "ids or titles to remove, and the reason.",
            schema=plan_delta_schema(),
            model_type=PlanDeltaDraft,
            problems=lambda value: self._validator.delta_problems(
                current, value, brief=brief, budget=resolved_budget
            ),
            reject=PlanRejected,
            reject_message="the planner could not produce a valid plan delta",
            budget=resolved_budget,
            digest=digest,
            iteration=current.version,
            append=payload,
        )
        delta = self._validator.apply_delta(
            current,
            draft,
            brief=brief,
            budget=resolved_budget,
            id_factory=self._id_factory,
            strategy_change=strategy_change,
        )
        self._plan = delta.apply_to(current)
        self._budget = resolved_budget
        return delta

    # -- evaluation --------------------------------------------------------
    async def evaluate(
        self,
        digest: str | DigestSnapshot | None,
        report: QualityReport | None,
        probes: ProbeReport,
        *,
        deep_check: bool = True,
        iteration: int | None = None,
    ) -> Verdict:
        """Judge the article state and return a contract-valid ``Verdict``.

        The verdict is stricter than the model's: any failed hard gate, a ``Q`` below
        ``policies.q_min``, or an open high-severity issue forces ``meets_minimum`` false
        and names the reason in ``missing``.

        Args:
            digest: the bounded ``claude.md`` slice.
            report: the validator's report, when one exists.
            probes: the deterministic gate results and compact view.
            deep_check: send the report's opt-in full text, at most once per distinct
                text (an unchanged deep check is replaced by a one-line note).
            iteration: loop iteration; defaults to the current budget's iteration.
        """
        evaluation = await self.evaluate_detailed(
            digest, report, probes, deep_check=deep_check, iteration=iteration
        )
        return evaluation.verdict

    async def evaluate_detailed(
        self,
        digest: str | DigestSnapshot | None,
        report: QualityReport | None,
        probes: ProbeReport,
        *,
        deep_check: bool = True,
        iteration: int | None = None,
    ) -> Evaluation:
        """Like :meth:`evaluate`, but also returns the scores, Q, gates and usage."""
        if self._brief is None:
            raise PlannerStateMissing(
                "evaluate needs the run's brief; call plan() first or set_brief()"
            )
        budget = self._budget or PlanBudget.from_policies(self._policies, iteration=iteration or 0)
        loop_iteration = budget.iteration if iteration is None else iteration
        payload = self._evaluation_payload(probes, report, deep_check=deep_check)
        draft, usage, attempts = await self._ask(
            kind=EVALUATE,
            role=AgentRole.VALIDATOR,
            instruction=(
                "Evaluate the article state against the brief and the rubric. The "
                "hard-gate results are facts; you cannot overturn them. Return only the "
                "evaluation JSON."
            ),
            schema=evaluation_schema(),
            model_type=EvaluationDraft,
            problems=lambda value: evaluation_problems(value, rubric=self._rubric),
            reject=EvaluationRejected,
            reject_message="the planner could not produce a valid evaluation",
            budget=budget,
            digest=digest,
            iteration=loop_iteration,
            append=payload,
        )
        scores = dict(draft.scores)
        verdict = merge_verdict(
            draft=draft,
            probes=probes,
            policies=self._policies,
            rubric=self._rubric,
            report_high_issues=len(report.high_severity_issues()) if report is not None else 0,
        )
        evaluation = Evaluation(
            verdict=verdict,
            scores=scores,
            q=weighted_q(scores, self._rubric),
            gates=probes.gate_map,
            failed_gates=tuple(probes.failed_gates),
            planner_meets_minimum=draft.meets_minimum,
            attempts=attempts,
            tokens=usage,
            deep_checked=tuple(probes.deep_sections) if deep_check else (),
        )
        self._last_evaluation = evaluation
        return evaluation

    # -- call machinery ----------------------------------------------------
    async def _ask(
        self,
        *,
        kind: str,
        role: AgentRole,
        instruction: str,
        schema: dict[str, Any],
        model_type: type[ModelT],
        problems: Callable[[ModelT], list[str]],
        reject: type[PlannerRejected],
        reject_message: str,
        budget: PlanBudget,
        digest: str | DigestSnapshot | None,
        iteration: int,
        append: str | None = None,
    ) -> tuple[ModelT, TokenUsage, int]:
        """Call the model, validate, and retry with the error list only.

        Raises:
            PlannerOutputInvalid: a reply was not JSON at all and no retry was left.
            PlannerRejected: validation failed on the first attempt plus every retry.
        """
        task = planner_task(
            purpose=kind,
            instructions=instruction,
            budget=budget,
            role=role,
            id_factory=self._id_factory,
        )
        run_state = PlannerRunState(brief=self._brief, iter=iteration, digest=_digest_text(digest))
        usage = TokenUsage(**_ZERO_USAGE)
        found: list[str] = []
        last_was_unparsable = False

        for attempt in range(self._max_retries + 1):
            override = None if attempt == 0 else retry_instruction(found)
            response = await self._call(
                kind=kind,
                task=task,
                run_state=run_state,
                instruction=instruction,
                schema=schema,
                override=override,
                append=None if override is not None else append,
            )
            usage = _usage_add(usage, response.usage)
            if budget.call_token_budget is not None and usage.total > budget.call_token_budget:
                raise BudgetExceeded(
                    f"planner call '{kind}' spent {usage.total} tokens; the call budget is "
                    f"{budget.call_token_budget}",
                    role=BindingRole.PLANNER.value,
                    kind=kind,
                    tokens=usage.total,
                    call_token_budget=budget.call_token_budget,
                )
            try:
                payload = parse_agent_json(response.text)
            except Exception as exc:  # parse_agent_json signals failure with a private type
                found = [f"output: {exc}"]
                last_was_unparsable = True
                continue
            try:
                draft = model_type.model_validate(payload)
            except ValidationError as exc:
                found = [f"schema: {format_validation_errors(exc, limit=8)}"]
                last_was_unparsable = False
                continue
            found = problems(draft)
            if not found:
                return draft, usage, attempt + 1
            last_was_unparsable = False

        if last_was_unparsable:
            raise PlannerOutputInvalid(
                f"planner call '{kind}' did not return a JSON object after "
                f"{self._max_retries + 1} attempt(s)",
                problems=found,
                attempts=self._max_retries + 1,
                role=BindingRole.PLANNER.value,
            )
        raise reject(
            reject_message,
            problems=found,
            attempts=self._max_retries + 1,
            tokens=usage,
        )

    async def _call(
        self,
        *,
        kind: str,
        task: Task,
        run_state: PlannerRunState,
        instruction: str,
        schema: dict[str, Any],
        override: str | None,
        append: str | None,
    ) -> LLMResponse:
        """One governed provider call, bounded by the binding's caps."""
        binding = self._resolve_binding()
        prompt = self._builders[kind].build(
            task=task,
            run_state=run_state,
            instruction=instruction,
            json_schema=schema,
            max_input_tokens=min(binding.max_input_tokens, self._policies.call_input_cap),
            max_output_tokens=self._output_cap(kind, binding),
            volatile_override=override,
            append=append,
        )
        self._enforce_caps(prompt, binding)
        self._governor.precheck(prompt.request, role=BindingRole.PLANNER, task=task)
        response = await self._provider.generate(
            prompt.request, model=binding.model, role=BindingRole.PLANNER.value
        )
        self._usage = _usage_add(self._usage, response.usage)
        self._calls += 1
        self._last_prompt = prompt
        return response

    def _output_cap(self, kind: str, binding: AgentBinding) -> int:
        """Completion ceiling for this kind of planner call."""
        ceiling = EVALUATOR_MAX_OUTPUT_TOKENS if kind == EVALUATE else PLANNER_MAX_OUTPUT_TOKENS
        return min(binding.max_output_tokens, ceiling)

    def _enforce_caps(self, prompt: PlannerRequest, binding: AgentBinding) -> None:
        """Refuse locally rather than send a prompt the binding forbids."""
        if prompt.input_tokens > binding.max_input_tokens:
            raise BudgetExceeded(
                f"planner prompt is ~{prompt.input_tokens} tokens; the binding allows "
                f"{binding.max_input_tokens}",
                role=BindingRole.PLANNER.value,
                prompt_tokens=prompt.input_tokens,
                max_input_tokens=binding.max_input_tokens,
            )
        if prompt.input_tokens > self._policies.call_input_cap:
            raise BudgetExceeded(
                f"planner prompt is ~{prompt.input_tokens} tokens; call_input_cap is "
                f"{self._policies.call_input_cap}",
                role=BindingRole.PLANNER.value,
                prompt_tokens=prompt.input_tokens,
                call_input_cap=self._policies.call_input_cap,
            )

    def _resolve_binding(self) -> AgentBinding:
        """Read the planner binding now - accounts resolve at call time."""
        binding = self._bindings.resolve(BindingRole.PLANNER)
        if binding is None or not binding.is_active:
            raise PlannerUnavailable(
                "no active binding for role 'planner'; add an Anthropic API key account "
                "and bind it to the planner",
                role=BindingRole.PLANNER.value,
            )
        if binding.role is not BindingRole.PLANNER:
            raise ContractViolation(
                f"binding resolved for 'planner' declares role '{binding.role.value}'",
                expected_role=BindingRole.PLANNER.value,
                binding_role=binding.role.value,
            )
        return binding

    # -- payloads ----------------------------------------------------------
    def _evaluation_payload(
        self,
        probes: ProbeReport,
        report: QualityReport | None,
        *,
        deep_check: bool,
    ) -> str:
        """The evaluation facts appended to the user message (never the whole article)."""
        gates = {gate.name: {"passed": gate.passed, "detail": gate.detail} for gate in probes.gates}
        parts = [
            "## Hard gates (deterministic; you cannot change these)",
            json.dumps(gates, sort_keys=True, separators=(",", ":")),
            "## Compact article view",
            probes.compact.render(),
        ]
        if report is not None:
            parts.append("## Validator report")
            parts.append(
                json.dumps(_report_snapshot(report), sort_keys=True, separators=(",", ":"))
            )
        if deep_check and probes.deep_text:
            fingerprint = content_hash(probes.deep_text)
            if fingerprint == self._deep_fingerprint:
                parts.append("## Full text (opt-in)")
                parts.append(
                    "[unchanged since the previous evaluation, so it is not re-sent; "
                    "judge the compact view]"
                )
            else:
                parts.append("## Full text (opt-in, bounded)")
                parts.append(probes.deep_text)
                self._deep_fingerprint = fingerprint
        return "\n\n".join(parts)

    def _replan_payload(
        self,
        plan: Plan,
        report: QualityReport | None,
        decision: LoopDecision | None,
        strategy_change: bool,
    ) -> str:
        """The current plan, the decision that triggered the replan and the report."""
        plan_view = {
            "version": plan.version,
            "goal": plan.goal,
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "role": task.role.value,
                    "section_ids": list(task.section_ids),
                    "token_budget": task.token_budget,
                    "depends_on": list(task.depends_on),
                }
                for task in plan.tasks
            ],
        }
        parts = [
            "## Current plan",
            json.dumps(plan_view, sort_keys=True, separators=(",", ":")),
        ]
        if decision is not None:
            parts.append("## Last loop decision")
            parts.append(
                json.dumps(
                    {
                        "iter": decision.iter,
                        "action": decision.action.value,
                        "reasons": list(decision.reasons),
                        "evidence": decision.evidence,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        if report is not None:
            parts.append("## Quality report")
            parts.append(
                json.dumps(_report_snapshot(report), sort_keys=True, separators=(",", ":"))
            )
        if strategy_change:
            parts.append("## Strategy change requested")
            parts.append(
                "The run has plateaued for the first time. Do not repeat the same task "
                "shape: change the approach (different research angle, narrower tasks, a "
                "reviewer pass before more drafting) and say why in `reason`."
            )
        return "\n\n".join(parts)

    def _coerce_budget(self, budget: PlanBudget | int) -> PlanBudget:
        """Accept a bare remaining-token count for callers that do not track iterations."""
        if isinstance(budget, PlanBudget):
            return budget
        if isinstance(budget, bool) or not isinstance(budget, int):
            raise TypeError(f"budget must be a PlanBudget or an int, got {type(budget).__name__}")
        return PlanBudget(remaining_run_tokens=budget)


def _report_snapshot(report: QualityReport) -> dict[str, Any]:
    """A bounded view of the validator's report for the planner prompt."""
    return {
        "iter": report.iter,
        "Q": report.Q,
        "verified_citation_ratio": report.verified_citation_ratio,
        "gates": dict(report.gates),
        "scores": dict(report.scores),
        "issues": [
            {
                "id": issue.id,
                "section_id": issue.section_id,
                "severity": issue.severity.value,
                "text": issue.text[:240],
            }
            for issue in report.issues[:MAX_PAYLOAD_ISSUES]
        ],
        "issues_total": len(report.issues),
    }
