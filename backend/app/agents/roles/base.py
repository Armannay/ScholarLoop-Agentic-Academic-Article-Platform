"""``RoleAgent``: the shared body of the five role agents, on the Step 5 runtime.

The generic :class:`~app.agents.base.Agent` executes one task with one provider call
and one optional repair. A role agent adds exactly five things and inherits the rest:

1. **A prompt asset** (``prompts/<role>.md``) appended to the cached system prefix. The
   rules files carry the hard rules; the prompt carries what this role is doing right
   now. Both are stable, so both are cacheable.
2. **A second, role-specific document next to the envelope.** The answer is
   ``{"result": AgentResult, "output": <role output>}`` and the request is constrained to
   a schema generated from both models, so the two can never drift apart. The role output
   carries what CONTRACTS v1 has no field for (goal and claims map, per-section scores,
   findings) - see CCR-7.1 in ``docs/arena-log/step-07/output.md``.
3. **Deterministic preparation** (:meth:`RoleAgent.prepare`): tool results for the
   researcher, section text for the writer/reviewer/validator, the gate pack for the
   validator. These travel as *system blocks computed for this run*, never as article
   text in the task message.
4. **Role acceptance checks** (:meth:`RoleAgent.output_problems`) run inside the runtime's
   validation step, so a violation earns the same single repair call as a schema error:
   unknown citation ids, a reviewer rewriting prose, a writer ignoring its word target.
5. **Token caps** as ceilings over the binding: ``max_input_tokens`` per prompt and
   ``max_output_tokens`` per answer. A binding may be more generous; it can never make a
   role spend more than its own cap.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from app.agents.base import (
    Agent,
    AgentProvider,
    BindingResolver,
    Governor,
    _InvalidOutput,
    _pydantic_problems,
    agent_output_schema,
)
from app.agents.context import AgentContext, ContextBuilder, RunState
from app.agents.errors import AgentTaskMismatch
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.common import Contract
from app.contracts.enums import AgentRole, ModelTier
from app.contracts.llm import LLMRequest, LLMResponse, SystemBlock
from app.contracts.task import Task
from app.core.errors import BudgetExceeded, ConfigError
from app.core.hashing import content_hash
from app.core.ids import new_ulid
from app.providers.base import prompt_tokens
from app.tools.base import ToolRegistry

__all__ = [
    "ROLE_PROMPT_DIR",
    "RoleAgent",
    "RolePromptContextBuilder",
    "RoleRun",
    "load_role_prompt",
    "repository_root",
    "role_call_schema",
    "role_prompt_path",
]

#: Where role prompt assets live, relative to the repository root.
ROLE_PROMPT_DIR: str = "prompts"


def repository_root() -> Path:
    """The repository root, found by walking up to the directory holding ``prompts/``.

    Resolving it by shape rather than by counting ``parents`` keeps the lookup correct
    whether the package is imported from a source checkout or an editable install, and
    fails loudly (instead of silently shipping a prompt-less agent) when neither.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ROLE_PROMPT_DIR).is_dir() and (parent / "rules").is_dir():
            return parent
    raise ConfigError(
        "cannot locate the repository root: no ancestor directory holds both "
        "'prompts/' and 'rules/'. Role prompt assets are required, not optional."
    )


def role_prompt_path(role: AgentRole, *, root: Path | None = None) -> Path:
    """Path of one role's prompt asset."""
    return (root or repository_root()) / ROLE_PROMPT_DIR / f"{role.value}.md"


def load_role_prompt(role: AgentRole, *, root: Path | None = None) -> str:
    """Read a role's prompt asset, or raise ``ConfigError`` when it is missing."""
    path = role_prompt_path(role, root=root)
    if not path.is_file():
        raise ConfigError(f"role prompt asset not found: {path}", role=role.value)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ConfigError(f"role prompt asset is empty: {path}", role=role.value)
    return text


# --------------------------------------------------------------------------- schemas
def _strip_fields(schema: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Remove properties (and their ``required`` entries) from a schema object."""
    if not fields:
        return schema
    stripped = copy.deepcopy(schema)
    properties = stripped.get("properties")
    if isinstance(properties, dict):
        for name in fields:
            properties.pop(name, None)
    required = stripped.get("required")
    if isinstance(required, list):
        stripped["required"] = [name for name in required if name not in fields]
    return stripped


def _prefix_defs(schema: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Rename ``$defs`` keys with ``prefix`` so two schemas can share one root.

    ``$ref`` strings are rewritten with a trailing quote in the match, which is what
    keeps ``#/$defs/Review`` from also rewriting ``#/$defs/ReviewerOutput``. The rename
    happens on the *already rewritten* definitions: a nested reference (``SectionReview``
    pointing at ``ReviewIssue``) must be rewritten too, or the composed schema would
    carry a ``$ref`` to a definition the prefix removed.
    """
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not definitions:
        return schema
    mapping = {name: f"{prefix}{name}" for name in definitions}
    text = json.dumps(schema)
    for old, new in mapping.items():
        text = text.replace(f'#/$defs/{old}"', f'#/$defs/{new}"')
    renamed: dict[str, Any] = json.loads(text)
    rewritten = renamed.get("$defs", {})
    renamed["$defs"] = {mapping[name]: value for name, value in rewritten.items()}
    return renamed


def role_call_schema(
    result_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    *,
    forbidden_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose ``{"result": AgentResult, "output": <role output>}`` into one schema.

    ``forbidden_fields`` are removed from the envelope's *model-facing* schema: a
    validator is never asked to invent a ``QualityReport`` (it is computed from tool
    output) and a reviewer is never offered a ``patch`` field. Removing the property
    with ``additionalProperties: false`` turns "must not" into "cannot".
    """
    result_part = _strip_fields(dict(result_schema), forbidden_fields)
    result_defs = result_part.pop("$defs", {})
    output_part = _prefix_defs(dict(output_schema), "Role")
    output_defs = output_part.pop("$defs", {})
    collisions = sorted(set(result_defs) & set(output_defs))
    if collisions:  # pragma: no cover - the Role prefix makes this unreachable
        raise ConfigError(f"role schema definitions collide with contracts: {collisions}")

    return {
        "title": "RoleAgentAnswer",
        "type": "object",
        "description": (
            "One role answer: 'result' is the AgentResult contract, 'output' is the "
            "role-specific document. Both are required."
        ),
        "properties": {"result": result_part, "output": output_part},
        "required": ["result", "output"],
        "additionalProperties": False,
        "$defs": {**result_defs, **output_defs},
    }


# ------------------------------------------------------------------------ context
@dataclass(frozen=True)
class RolePromptContextBuilder:
    """Wrap a context builder and append the role's prompt as a cached block.

    Appending (rather than prepending) keeps the wrapped builder's prefix byte-identical
    for every role, so a shared ``ContextBuilder`` still produces one cacheable prefix
    per role and the prompt is the only thing that differs.
    """

    inner: ContextBuilder
    prompt: str

    def build(self, *, task: Task, run_state: RunState) -> AgentContext:
        """Build the inner context, then add the role prompt block."""
        context = self.inner.build(task=task, run_state=run_state)
        blocks = [*context.system_blocks, SystemBlock(text=self.prompt, cache=True)]
        return AgentContext(
            system_blocks=tuple(blocks),
            messages=context.messages,
            skills=context.skills,
            prefix_hash=content_hash("\n\n".join(block.text for block in blocks)),
        )


# ---------------------------------------------------------------------------- runs
@dataclass
class RoleRun:
    """Per-run scratch: blocks computed in ``prepare``, consumed by the role's checks."""

    blocks: list[SystemBlock] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    output: Contract | None = None

    def block(self, title: str, body: str) -> None:
        """Add one labelled block to the prompt, bounded by the caller's formatting."""
        self.blocks.append(SystemBlock(text=f"## {title}\n{body}", cache=False))

    def json_block(self, title: str, payload: Mapping[str, Any]) -> None:
        """Add one JSON block: structured input the model reads and cannot misparse."""
        self.block(title, json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


class RoleAgent(Agent):
    """Shared base for architect, researcher, writer, reviewer and validator."""

    #: A shared base, never executed: each concrete role sets ``role`` and ``output_model``.
    abstract_agent_base: ClassVar[bool] = True
    #: Default model tier for this role (policies.yaml names the same tier).
    default_tier: ClassVar[ModelTier] = ModelTier.MID
    #: Tools this role may use. Mirrors ``config/tool_policy.yaml`` (a test pins that).
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    #: Prompt ceiling for one call of this role, below any binding cap.
    max_input_tokens: ClassVar[int] = 16_000
    #: Answer ceiling for one call of this role, below any binding cap.
    max_output_tokens: ClassVar[int] = 4_000
    #: The role-specific document that travels next to the envelope.
    output_model: ClassVar[type[Contract]]
    #: Envelope fields this role may *not* fill: removed from the model-facing schema.
    forbidden_result_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        provider: AgentProvider,
        bindings: BindingResolver,
        governor: Governor,
        context_builder: ContextBuilder,
        prompt: str | None = None,
        tools: ToolRegistry | None = None,
        project: str | None = None,
        id_factory: Callable[[], str] = new_ulid,
    ) -> None:
        if not isinstance(getattr(type(self), "output_model", None), type):
            raise TypeError("RoleAgent subclasses must set output_model")
        self._runs: dict[str, RoleRun] = {}
        self._tools = tools
        self._project = project
        self._prompt = prompt if prompt is not None else load_role_prompt(self.role)
        self._call_schema = role_call_schema(
            agent_output_schema(),
            self.output_model.model_json_schema(mode="validation"),
            forbidden_fields=self.forbidden_result_fields,
        )
        super().__init__(
            provider=provider,
            bindings=bindings,
            governor=governor,
            context_builder=RolePromptContextBuilder(inner=context_builder, prompt=self._prompt),
            id_factory=id_factory,
        )

    # -- metadata ---------------------------------------------------------
    @property
    def tier(self) -> ModelTier:
        """Default model tier for this role (a binding may override the model)."""
        return self.default_tier

    @property
    def tools(self) -> ToolRegistry | None:
        """The scoped tool registry this role may call, when one was injected."""
        return self._tools

    @property
    def project(self) -> str | None:
        """Project scope used for tool authorisation."""
        return self._project

    @property
    def prompt(self) -> str:
        """The role prompt asset, as it appears in the system prefix."""
        return self._prompt

    def call_schema(self) -> dict[str, Any]:
        """The JSON schema this role's answer is constrained to (defensive copy)."""
        return copy.deepcopy(self._call_schema)

    def _output_schema(self) -> dict[str, Any]:
        """The runtime's schema hook: the combined role schema, not the bare envelope."""
        return self.call_schema()

    # -- execution --------------------------------------------------------
    async def run(self, task: Task, run_state: RunState) -> AgentResult:
        """Fail fast on an out-of-scope task, then run the Step 5 pipeline unchanged."""
        self.check_task(task, run_state)
        try:
            return await super().run(task, run_state)
        finally:
            # A failed run must not leave scratch behind for the next iteration.
            self._runs.pop(task.id, None)

    def check_task(self, task: Task, run_state: RunState) -> None:
        """Role scope rules that do not need a model. Raises before any token is spent."""
        if task.role is not self.role:
            raise AgentTaskMismatch(
                f"agent role '{self.role.value}' cannot execute a task owned by "
                f"'{task.role.value}'",
                agent_role=self.role.value,
                task_role=task.role.value,
                task_id=task.id,
            )
        _ = run_state

    async def prepare(self, task: Task, run_state: RunState) -> Sequence[SystemBlock]:
        """Create this run's scratch and return no blocks (roles override)."""
        _ = run_state
        self._scratch(task)
        return ()

    def _scratch(self, task: Task) -> RoleRun:
        """This run's scratch object, created on first use."""
        return self._runs.setdefault(task.id, RoleRun())

    def _build_request(
        self,
        context: AgentContext,
        binding: AgentBinding,
        *,
        extra_blocks: Sequence[SystemBlock] = (),
    ) -> LLMRequest:
        """Clamp the answer to the role's own ceiling, then build the request."""
        request = super()._build_request(context, binding, extra_blocks=extra_blocks)
        capped = min(binding.max_output_tokens, self.max_output_tokens)
        if capped != request.max_output_tokens:
            return request.model_copy(update={"max_output_tokens": capped})
        return request

    async def _generate(
        self, request: LLMRequest, binding: AgentBinding, task: Task
    ) -> LLMResponse:
        """Enforce the role's prompt ceiling before the binding's own cap is consulted."""
        estimate = prompt_tokens(request)
        if estimate > self.max_input_tokens:
            raise BudgetExceeded(
                f"prompt for role '{self.role.value}' is ~{estimate} tokens; the role "
                f"ceiling is {self.max_input_tokens}",
                role=self.role.value,
                task_id=task.id,
                prompt_tokens=estimate,
                role_max_input_tokens=self.max_input_tokens,
            )
        return await super()._generate(request, binding, task)

    def _validate_document(
        self, payload: dict[str, Any], *, raw_text: str, task: Task, run_state: RunState
    ) -> AgentResult:
        """Validate the envelope, then the role document, then the role's own checks."""
        raw_result = payload.get("result")
        raw_output = payload.get("output")
        if not isinstance(raw_result, dict):
            raise _InvalidOutput(
                ["result: required object is missing; return {'result': {...}, 'output': {...}}"]
            )
        if not isinstance(raw_output, dict):
            raise _InvalidOutput(["output: required object is missing"])

        try:
            output = self.output_model.model_validate(raw_output)
        except ValidationError as exc:
            raise _InvalidOutput(
                [f"output.{problem}" for problem in _pydantic_problems(exc)]
            ) from exc

        result = super()._validate_document(
            raw_result, raw_text=raw_text, task=task, run_state=run_state
        )
        problems = [
            *self._forbidden_field_problems(result),
            *self.output_problems(output, result=result, task=task, run_state=run_state),
        ]
        if problems:
            raise _InvalidOutput(problems)
        self._scratch(task).output = output
        return result

    # -- role hooks -------------------------------------------------------
    def _forbidden_field_problems(self, result: AgentResult) -> list[str]:
        """Enforce ``forbidden_result_fields`` on the parsed answer, not just its schema.

        The model-facing schema already omits these properties, which is the strong
        guarantee; this is the cheap second line, for a provider that ignores the schema
        (or for a caller that replays a hand-written answer).
        """
        problems: list[str] = []
        for name in self.forbidden_result_fields:
            value = getattr(result, name, None)
            if value is None or (isinstance(value, (list, tuple, dict)) and not value):
                continue
            problems.append(
                f"result.{name}: the {self.role.value} role may not return {name}; it is "
                "computed by the runtime or owned by another role"
            )
        return problems

    def output_problems(
        self,
        output: Contract,
        *,
        result: AgentResult,
        task: Task,
        run_state: RunState,
    ) -> list[str]:
        """Acceptance checks the schema cannot express. Empty by default."""
        _ = (output, result, task, run_state)
        return []

    def take_output(self, task: Task) -> Contract | None:
        """Pop the validated role document for this run (``None`` outside a run)."""
        run = self._runs.get(task.id)
        return None if run is None else run.output

    def require_brief(self, run_state: RunState) -> None:
        """Refuse to run a role whose whole job depends on a Brief that is missing."""
        if run_state.brief is None:
            raise ConfigError(
                f"role '{self.role.value}' needs a Brief in the run state to do its job",
                role=self.role.value,
            )
