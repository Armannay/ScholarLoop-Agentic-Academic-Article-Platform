"""Golden fixtures: one worked input -> expected answer per role.

``prompts/<role>.golden.json`` is both documentation and a test fixture. Each file holds

* ``input`` - the task, the iteration and the run state a role would receive, in real
  contract shapes (``Task``, ``Brief``, ``SectionState``, ``SourceCard``), and
* ``expected`` - the exact JSON answer the model is expected to produce: ``result`` (the
  ``AgentResult`` envelope) and ``output`` (the role document).

The suite feeds ``expected`` straight to a ``MockProvider``, so a fixture that no longer
validates is a failing test rather than a stale example. Because a coarse fixture proves
little, each one is written as a *complete* answer: an architect whose word targets sum
to the brief, a writer whose citations resolve, a reviewer whose scores cover the rubric.

Loading is strict: an unknown key anywhere is an error, which keeps a typo in a fixture
from silently disabling the check it was meant to drive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from app.agents.context import AgentRunState
from app.agents.roles.base import repository_root
from app.agents.roles.inputs import MappingSectionTexts
from app.contracts.brief import Brief
from app.contracts.common import Contract, SectionId
from app.contracts.enums import AgentRole
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError, format_validation_errors

__all__ = [
    "GoldenCase",
    "GoldenExpected",
    "GoldenInput",
    "golden_path",
    "load_golden",
    "load_goldens",
    "scripted_answer",
]


class GoldenText(Contract):
    """One section's body text in a fixture.

    ``str_strip_whitespace`` and ``str_min_length`` are relaxed here for the same reason
    ``PatchOp`` relaxes them: an empty section body is a legitimate state (nothing has
    been drafted yet), not a malformed field.
    """

    model_config = ConfigDict(str_min_length=0, str_strip_whitespace=False)

    id: SectionId
    text: str = ""


class GoldenInput(Contract):
    """The run a role would be handed: task, iteration, brief, sections, sources, text."""

    task: Task
    iter: int = 0
    brief: Brief | None = None
    sections: list[SectionState] = Field(default_factory=list)
    sources: list[SourceCard] = Field(default_factory=list)
    texts: list[GoldenText] = Field(
        default_factory=list,
        description="Section bodies, as the injected text provider would answer.",
    )


class GoldenExpected(Contract):
    """The answer: the envelope plus the role document, exactly as the model must emit."""

    result: dict[str, Any]
    output: dict[str, Any]


class GoldenCase(Contract):
    """One role's golden fixture."""

    role: AgentRole
    input: GoldenInput
    expected: GoldenExpected
    notes: str | None = None

    def run_state(self) -> AgentRunState:
        """The fixture's run state, as the runtime consumes it."""
        return AgentRunState(
            iter=self.input.iter,
            brief=self.input.brief,
            sections=tuple(self.input.sections),
            sources=tuple(self.input.sources),
        )

    def section_texts(self) -> MappingSectionTexts:
        """The fixture's section text, as the provider a role reads."""
        return MappingSectionTexts({entry.id: entry.text for entry in self.input.texts})

    def answer_json(self) -> str:
        """The expected answer as the JSON text a provider would return."""
        return json.dumps(
            {"result": self.expected.result, "output": self.expected.output},
            ensure_ascii=False,
            sort_keys=True,
        )


def golden_path(role: AgentRole, *, root: Path | None = None) -> Path:
    """Path of one role's golden fixture."""
    return (root or repository_root()) / "prompts" / f"{role.value}.golden.json"


def load_golden(role: AgentRole, *, root: Path | None = None) -> GoldenCase:
    """Load and validate one fixture, naming the offending field on failure."""
    path = golden_path(role, root=root)
    if not path.is_file():
        raise ConfigError(f"golden fixture not found: {path}", role=role.value)
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"golden fixture is not valid JSON: {path}: {exc}") from exc
    try:
        case = GoldenCase.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid golden fixture {path} [{format_validation_errors(exc)}]"
        ) from exc
    if case.role is not role:
        raise ConfigError(
            f"golden fixture {path} declares role '{case.role.value}', expected '{role.value}'"
        )
    return case


def load_goldens(*, root: Path | None = None) -> dict[AgentRole, GoldenCase]:
    """Load every role's fixture, keyed by role. A missing file is an error."""
    return {role: load_golden(role, root=root) for role in AgentRole}


def scripted_answer(case: GoldenCase) -> str:
    """The fixture's answer, ready to hand to ``MockProvider.script_reply``."""
    return case.answer_json()
