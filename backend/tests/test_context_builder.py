"""The task context: a bounded prompt made of references, never of article text.

HARD RULE 3 in one module. The builder is the seam between the orchestrator (which
owns the article, the sources and the digest) and the agent runtime (which owns the
prompt). What is tested here:

* the rules are the stable, cacheable prefix;
* the brief and the task are always present, the acceptance criteria included;
* sections appear as *state*, sources as *cards* - never as prose;
* everything is capped, so a prompt cannot grow without bound;
* a missing brief is an error, never a guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.context import ContextError, TaskContext, TaskContextBuilder
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole
from app.contracts.section import CitationRef, SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from tests.factories import SAMPLE_BRIEF, sample_ulid

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


@pytest.fixture
def brief() -> Brief:
    """The sample brief from the test factories."""
    return Brief.model_validate(SAMPLE_BRIEF)


@pytest.fixture
def task() -> Task:
    """A writer task over one section."""
    return Task(
        id=sample_ulid(1),
        title="Draft the introduction",
        role=AgentRole.WRITER,
        section_ids=["introduction"],
        source_ids=[sample_ulid(2)],
        instructions="Open with the gap, then state the contribution.",
        acceptance=["No unsupported claims", "Ends with the contribution statement"],
        token_budget=6_000,
    )


@pytest.fixture
def context(brief: Brief) -> TaskContext:
    """A run state with one section and one source card."""
    section = SectionState(
        id="introduction",
        title="Introduction",
        order=0,
        version=2,
        words=412,
        citations=[CitationRef(key="(Nguyen, 2020)", source_id=sample_ulid(2))],
        status="draft",
    )
    card = SourceCard(
        id=sample_ulid(2),
        title="Budgeted multi-agent writing",
        authors=["Nguyen, L."],
        year=2024,
        venue="Journal of Applied NLP Systems",
        doi="10.1234/jans.2024.00042",
        snippet="A bounded controller keeps prompt growth linear in the number of iterations.",
    )
    return TaskContext(
        brief=brief,
        sections=[section],
        sources=[card],
        digest="iter 1: writer drafted the introduction (412 words)",
        iteration=1,
    )


@pytest.fixture
def builder(repo_root: Path) -> TaskContextBuilder:
    """A builder that reads the real rules files from the repository."""
    return TaskContextBuilder(
        rules_dir=repo_root / "rules",
        max_output_tokens=2_000,
        json_schema=OUTPUT_SCHEMA,
    )


def prompt_text(request: object) -> str:
    """The text of the request's user message."""
    from app.contracts.llm import LLMRequest

    assert isinstance(request, LLMRequest)
    return request.messages[0].text


# --------------------------------------------------------------------------- shape
def test_the_rules_are_the_stable_prefix(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """The root rules and the role rules come first and are cacheable."""
    request = builder.build(task, context)
    assert len(request.system_blocks) == 3
    assert request.system_blocks[0].cache is True
    assert "Never fabricate sources" in request.system_blocks[0].text
    assert "Writer rules" in request.system_blocks[1].text
    assert request.system_blocks[1].cache is True


def test_the_task_and_its_acceptance_criteria_are_present(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """The agent is graded against the acceptance list, so it must see it."""
    text = prompt_text(builder.build(task, context))
    assert task.title in text
    assert task.instructions in text
    for criterion in task.acceptance:
        assert criterion in text


def test_the_brief_is_present(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """Topic, audience, language and citation style steer the prose."""
    text = prompt_text(builder.build(task, context))
    assert context.brief.topic in text
    assert context.brief.audience in text
    assert context.brief.citation_style.value in text
    assert context.brief.language in text


def test_sections_appear_as_state_not_as_text(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """The prompt carries ids and metrics; the prose stays in the article store."""
    text = prompt_text(builder.build(task, context))
    assert "introduction" in text
    assert "412 words" in text
    assert "references only" in text


def test_sources_appear_as_cards(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """A card carries the locator an agent needs to cite it."""
    text = prompt_text(builder.build(task, context))
    assert context.sources[0].id in text
    assert "10.1234/jans.2024.00042" in text
    assert "Nguyen, L." in text


def test_the_digest_is_included(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """The bounded history is part of the prompt."""
    text = prompt_text(builder.build(task, context))
    assert "iter 1: writer drafted" in text


def test_the_output_schema_is_attached(
    builder: TaskContextBuilder, task: Task, context: TaskContext
) -> None:
    """A structured answer is requested with a schema, not with prose."""
    request = builder.build(task, context)
    assert request.json_schema == OUTPUT_SCHEMA
    assert request.max_output_tokens == 2_000


# --------------------------------------------------------------------------- bounds
def test_the_number_of_sources_is_capped(repo_root: Path, task: Task, brief: Brief) -> None:
    """A long source list is truncated rather than pasted in full."""
    builder = TaskContextBuilder(rules_dir=repo_root / "rules", max_sources=3)
    cards = [
        SourceCard(
            id=sample_ulid(index),
            title=f"Source {index}",
            year=2020 + index,
            venue="Venue",
            url=f"https://example.org/{index}",
            snippet="A short supporting snippet.",
        )
        for index in range(10)
    ]
    context = TaskContext(brief=brief, sources=cards)
    text = prompt_text(builder.build(task, context))
    assert text.count("snippet:") == 3


def test_the_digest_is_truncated(repo_root: Path, task: Task, brief: Brief) -> None:
    """A long digest is cut to its bounded size."""
    builder = TaskContextBuilder(rules_dir=repo_root / "rules")
    context = TaskContext(brief=brief, digest="x" * 20_000)
    text = prompt_text(builder.build(task, context))
    assert text.count("x") <= 4_000


def test_a_context_without_a_brief_is_an_error(repo_root: Path, task: Task) -> None:
    """Without the brief an agent would have to guess - so it refuses."""
    builder = TaskContextBuilder(rules_dir=repo_root / "rules")
    with pytest.raises(ContextError):
        builder.build(task, {})


def test_a_missing_rules_file_is_a_config_error(tmp_path: Path, task: Task, brief: Brief) -> None:
    """The rules are not optional: a missing file must be loud."""
    builder = TaskContextBuilder(rules_dir=tmp_path / "nowhere")
    with pytest.raises(Exception) as excinfo:
        builder.build(task, TaskContext(brief=brief))
    assert "rules file not found" in str(excinfo.value)


def test_the_run_state_may_be_a_plain_mapping(repo_root: Path, task: Task, brief: Brief) -> None:
    """The orchestrator may hand over a mapping; the builder copes."""
    builder = TaskContextBuilder(rules_dir=repo_root / "rules")
    request = builder.build(task, {"brief": brief, "iteration": 2})
    assert brief.topic in prompt_text(request)


def test_the_rules_are_read_once_per_file_change(repo_root: Path, task: Task, brief: Brief) -> None:
    """The rules are cached, so a build does not hit the disk every time."""
    builder = TaskContextBuilder(rules_dir=repo_root / "rules")
    context = TaskContext(brief=brief)
    first = builder.build(task, context)
    second = builder.build(task, context)
    assert first.system_blocks[0].text == second.system_blocks[0].text
