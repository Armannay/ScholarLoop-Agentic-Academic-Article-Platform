"""Generic, structured-output agent runtime.

The runtime owns no article store and has no method that can mutate one: successful
article changes cross the boundary only as validated ``Patch`` objects. The provider
pool, context builder and governor are structural dependencies so this module can be
implemented before their concrete services arrive.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, Final, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.common import IssueId, WordBounded60
from app.contracts.enums import AgentRole, ChangeStatus, MessageRole
from app.contracts.ledger import ChangeEntry, TokenUsage, patch_ref_for
from app.contracts.llm import LLMMessage, LLMRequest, LLMResponse, SystemBlock
from app.contracts.patch import Patch
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.ids import new_ulid
from app.providers.base import LLMProvider, ProviderRouting

__all__ = [
    "AGENT_OUTPUT_JSON_SCHEMA",
    "Agent",
    "AgentBindingResolver",
    "AgentContextBuilder",
    "AgentGovernor",
    "AgentOutput",
    "AgentProviderPool",
]


class AgentOutput(BaseModel):
    """LLM-authored content only; runtime-owned ledger metadata is added locally.

    Keeping ids, iteration and token usage out of model output avoids asking an LLM to
    invent bookkeeping values. The returned object is still the frozen ``AgentResult``
    contract, validated after this content envelope is assembled.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)

    summary: WordBounded60 = Field(description="Concise change summary, at most 60 words.")
    patches: list[Patch] = Field(default_factory=list, max_length=64)
    source_cards: list[SourceCard] = Field(default_factory=list, max_length=64)
    sections: list[SectionState] = Field(default_factory=list, max_length=64)
    report: QualityReport | None = None
    status: ChangeStatus = ChangeStatus.OK
    issues_opened: list[IssueId] = Field(default_factory=list, max_length=64)
    issues_closed: list[IssueId] = Field(default_factory=list, max_length=64)


AGENT_OUTPUT_JSON_SCHEMA: Final[dict[str, Any]] = AgentOutput.model_json_schema(mode="validation")


class AgentContextBuilder(Protocol):
    """Build one task-specific request; stable skill indexes belong in its prefix."""

    def build(self, task: Task, run_state: object) -> LLMRequest | Awaitable[LLMRequest]: ...


class AgentGovernor(Protocol):
    """Reject a call before it reaches a provider when its budgets do not allow it."""

    def precheck(
        self, request: LLMRequest, binding: AgentBinding, run_state: object
    ) -> object | Awaitable[object]: ...


class AgentBindingResolver(Protocol):
    """Resolve the current binding for a role; implementations must not cache it."""

    def resolve_binding(self, role: str) -> AgentBinding: ...


class AgentProviderPool(ProviderRouting, Protocol):
    """Resolve a provider from a fresh binding for each call, including repairs."""

    def get_provider(self, binding: AgentBinding) -> LLMProvider | Awaitable[LLMProvider]: ...


T = TypeVar("T")


async def _await_if_needed(value: T | Awaitable[T]) -> T:
    """Accept synchronous test doubles and asynchronous production adapters."""
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


class _OutputValidationError(ValueError):
    """A concise, input-free error safe to send in the single repair request."""


_OUTPUT_REPAIR_INSTRUCTION: Final[str] = (
    "Repair the previous JSON output using only the supplied validation error. "
    "Return one JSON object conforming to the supplied schema, with no commentary."
)


class Agent:
    """Run one task through context building, budget precheck and structured generation.

    ``Agent.run`` is async because ``LLMProvider.generate`` is async. ``provider_pool``
    must implement ``resolve_binding(role)`` and ``get_provider(binding)``. Bindings and
    providers are resolved afresh for the initial call and the optional repair call.
    """

    ROLE: ClassVar[AgentRole | None] = None

    def __init__(
        self,
        context_builder: AgentContextBuilder,
        governor: AgentGovernor,
        provider_pool: AgentProviderPool,
        *,
        binding_resolver: AgentBindingResolver | None = None,
        role: AgentRole | None = None,
    ) -> None:
        self.context_builder = context_builder
        self.governor = governor
        self.provider_pool = provider_pool
        self.binding_resolver = binding_resolver
        self.role = role if role is not None else self.ROLE

    async def run(self, task: Task, run_state: object) -> AgentResult:
        """Return a validated ``AgentResult``; make at most one validation repair call."""
        zero_usage = TokenUsage(in_=0, out=0, cached=0)
        if self.role is not None and self.role is not task.role:
            return self._failure(task, run_state, zero_usage)

        try:
            context = await _await_if_needed(self.context_builder.build(task, run_state))
            if not isinstance(context, LLMRequest):
                raise TypeError("ContextBuilder must return an LLMRequest")
        except Exception:
            return self._failure(task, run_state, zero_usage)

        try:
            first_response = await self._generate(context, task, run_state)
        except Exception:
            return self._failure(task, run_state, zero_usage)

        usage = first_response.usage
        try:
            return self._validated_result(first_response.text, task, run_state, usage)
        except (ValidationError, _OutputValidationError) as error:
            validation_error = self._format_validation_error(error)

        # Deliberately pass no original context here. The repair payload contains only
        # the validation error and the exact previous model output.
        try:
            repair_request = self._repair_request(validation_error, first_response.text)
            repair_response = await self._generate(repair_request, task, run_state)
        except Exception:
            return self._failure(task, run_state, usage)

        usage = self._sum_usage(usage, repair_response.usage)
        try:
            return self._validated_result(repair_response.text, task, run_state, usage)
        except (ValidationError, _OutputValidationError):
            return self._failure(task, run_state, usage)

    async def _generate(self, context: LLMRequest, task: Task, run_state: object) -> LLMResponse:
        """Resolve a fresh binding/provider, precheck, then perform one structured call."""
        resolver = (
            self.binding_resolver if self.binding_resolver is not None else self.provider_pool
        )
        binding = resolver.resolve_binding(task.role.value)
        if binding.role.value != task.role.value:
            raise ValueError("resolved binding role does not match the task role")
        if not binding.is_active:
            raise ValueError("resolved binding is disabled or has no active accounts")

        request = context.model_copy(
            update={
                "max_output_tokens": binding.max_output_tokens,
                "json_schema": dict(AGENT_OUTPUT_JSON_SCHEMA),
            },
            deep=True,
        )
        await _await_if_needed(self.governor.precheck(request, binding, run_state))
        provider = await _await_if_needed(self.provider_pool.get_provider(binding))
        return await provider.generate(
            request,
            model=binding.model,
            role=task.role.value,
        )

    @staticmethod
    def _repair_request(validation_error: str, previous_output: str) -> LLMRequest:
        """Create a context-free, schema-constrained single repair request."""
        payload = json.dumps(
            {"validation_error": validation_error, "previous_output": previous_output},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return LLMRequest(
            system_blocks=[SystemBlock(text=_OUTPUT_REPAIR_INSTRUCTION, cache=False)],
            messages=[LLMMessage(role=MessageRole.USER, text=payload)],
            # _generate replaces this with the freshly resolved binding's output cap.
            max_output_tokens=64,
            json_schema=dict(AGENT_OUTPUT_JSON_SCHEMA),
        )

    @classmethod
    def _validated_result(
        cls,
        raw_output: str,
        task: Task,
        run_state: object,
        usage: TokenUsage,
    ) -> AgentResult:
        """Validate JSON, patch scope and the final frozen contract."""
        try:
            decoded: object = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise _OutputValidationError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(decoded, dict):
            raise _OutputValidationError("the JSON output must be an object")

        content = AgentOutput.model_validate(decoded)
        if content.status is not ChangeStatus.OK and content.patches:
            raise _OutputValidationError("non-ok output status cannot include article patches")
        allowed_sections = set(task.section_ids)
        outside_scope = sorted({patch.section_id for patch in content.patches} - allowed_sections)
        if outside_scope:
            raise _OutputValidationError(
                "patch section_ids are outside task.section_ids: " + ", ".join(outside_scope)
            )

        patches = cls._ensure_patch_ids(content.patches)
        targets = sorted(
            {patch.section_id for patch in patches}
            | {section.id for section in content.sections}
            | {source.id for source in content.source_cards}
        )
        patch_ref: str | None = None
        if patches:
            first_patch_id = patches[0].patch_id
            if first_patch_id is None:
                raise _OutputValidationError("runtime did not assign a patch id")
            patch_ref = patch_ref_for(first_patch_id)
        entry = ChangeEntry(
            id=new_ulid(),
            iter=cls._iteration(run_state),
            task_id=task.id,
            role=task.role,
            targets=targets,
            summary=content.summary,
            patch_ref=patch_ref,
            tokens=usage,
            issues_opened=content.issues_opened,
            issues_closed=content.issues_closed,
            status=content.status,
        )
        return AgentResult(
            entry=entry,
            patches=patches,
            source_cards=content.source_cards,
            sections=content.sections,
            report=content.report,
        )

    @staticmethod
    def _ensure_patch_ids(patches: list[Patch]) -> list[Patch]:
        """Assign collision-free patch ids locally so the ledger can store a reference."""
        seen: set[str] = set()
        result: list[Patch] = []
        for patch in patches:
            patch_id = patch.patch_id
            if patch_id is None or patch_id in seen:
                patch_id = new_ulid()
            seen.add(patch_id)
            values = patch.model_dump(mode="python")
            values["patch_id"] = patch_id
            result.append(Patch.model_validate(values))
        return result

    @staticmethod
    def _iteration(run_state: object) -> int:
        """Read the current iteration without imposing a run-state contract."""
        if isinstance(run_state, Mapping):
            value = run_state.get("iter", run_state.get("iteration", 0))
        else:
            value = getattr(run_state, "iter", getattr(run_state, "iteration", 0))
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return min(max(value, 0), 10_000)

    @staticmethod
    def _sum_usage(*usages: TokenUsage) -> TokenUsage:
        """Aggregate non-overlapping usage over the initial and repair calls."""
        return TokenUsage(
            in_=sum(item.in_ for item in usages),
            out=sum(item.out for item in usages),
            cached=sum(item.cached for item in usages),
        )

    @classmethod
    def _format_validation_error(cls, error: ValidationError | _OutputValidationError) -> str:
        """Return a bounded error description without echoing Pydantic input values."""
        if isinstance(error, _OutputValidationError):
            return str(error)[:1000]
        entries: list[dict[str, object]] = []
        all_errors = error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
        for item in all_errors[:20]:
            entries.append(
                {
                    "location": [str(part) for part in item.get("loc", ())],
                    "message": str(item.get("msg", "invalid value"))[:300],
                    "type": str(item.get("type", "validation_error")),
                }
            )
        if len(all_errors) > len(entries):
            entries.append({"omitted_errors": len(all_errors) - len(entries)})
        return json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _failure(task: Task, run_state: object, usage: TokenUsage) -> AgentResult:
        """Construct a clean failure without leaking provider errors or raw model text."""
        entry = ChangeEntry(
            id=new_ulid(),
            iter=Agent._iteration(run_state),
            task_id=task.id,
            role=task.role,
            targets=[],
            summary="Agent execution did not produce a valid result.",
            patch_ref=None,
            tokens=usage,
            issues_opened=[],
            issues_closed=[],
            status=ChangeStatus.FAILED,
        )
        return AgentResult(entry=entry)
