"""Context assembly: layout, byte-stable prefix, mandatory rules and trim order.

The trim tests are parametric: they walk the input ceiling downwards and assert the
invariants that must hold at *every* limit - never truncate the task or the section
being edited, never reorder the trim steps, never change the stable prefix. Then three
targeted tests pin the individual steps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, MessageRole, SectionStatus
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import ConfigError
from app.core.hashing import content_hash
from app.core.rubric import Rubric
from app.governor import (
    TASK_INSTRUCTION,
    TRIM_ORDER,
    ContextBuilder,
    ContextTooLarge,
    MandatoryRuleGuard,
    MandatoryRuleRemoved,
    RuleBundle,
    RuleLibrary,
    TrimStep,
    mandatory_rules,
)
from app.providers.base import estimate_tokens, prompt_tokens
from app.skills.loader import SkillLoader
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.governor

SECTION_TEXT = (
    "Agentic writing loops trade tokens for quality. "
    "A deterministic orchestrator can measure that trade instead of guessing at it. "
) * 8
SECTION_TEXT = SECTION_TEXT.strip()
SECTION_ID = "introduction"


def _task_json(task: Task) -> str:
    """The exact task rendering the builder places in the volatile message."""
    return json.dumps(task.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class FakeRunState:
    """A run state with the optional extras the builder reads defensively."""

    brief: Brief | None = None
    iter: int = 2
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()
    digest: str = ""
    texts: Mapping[str, str] = field(default_factory=dict)

    def section_text(self, section_id: str) -> str:
        return self.texts.get(section_id, "")


@dataclass(frozen=True)
class MappingOnlyRunState:
    """A run state that exposes only ``section_texts``: the other documented shape."""

    brief: Brief | None = None
    iter: int = 0
    sections: tuple[SectionState, ...] = ()
    sources: tuple[SourceCard, ...] = ()
    section_texts: Mapping[str, str] = field(default_factory=dict)


def make_section(text: str = SECTION_TEXT) -> SectionState:
    return SectionState(
        id=SECTION_ID,
        title="Introduction",
        order=0,
        version=2,
        hash=content_hash(text),
        words=len(text.split()),
        status=SectionStatus.DRAFT,
    )


def make_card() -> SourceCard:
    return SourceCard(
        id=sample_ulid(2),
        title="Token-frugal agents",
        authors=["Nguyen, A."],
        year=2024,
        venue="Journal of Agentic Systems",
        doi="10.1234/jas.2024.7",
        snippet=(
            "Forty agentic runs show hierarchical token budgets cut spend by 38 percent "
            "without lowering reviewer scores."
        ),
        verification="verified",
    )


def make_task(
    *,
    role: AgentRole = AgentRole.WRITER,
    section_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    seed: int = 1,
) -> Task:
    card = make_card()
    return Task(
        id=sample_ulid(seed),
        title=f"Draft the introduction (task {seed})",
        role=role,
        section_ids=[SECTION_ID] if section_ids is None else section_ids,
        source_ids=[card.id] if source_ids is None else source_ids,
        instructions=(
            "Draft the introduction in IMRaD structure with APA citation, returning "
            "patch ops only. Stay inside the section word range."
        ),
        acceptance=["Cites the source card", "Stays inside the rubric word range"],
        token_budget=20_000,
    )


@pytest.fixture
def brief() -> Brief:
    return Brief.model_validate(SAMPLE_BRIEF)


@pytest.fixture
def skills(repo_root: Path) -> SkillLoader:
    return SkillLoader(repo_root / "skills")


@pytest.fixture
def rule_library(repo_root: Path) -> RuleLibrary:
    return RuleLibrary(repo_root / "rules")


@pytest.fixture
def builder(rule_library: RuleLibrary, rubric: Rubric, skills: SkillLoader) -> ContextBuilder:
    return ContextBuilder(
        rules=rule_library, rubric=rubric, skills=skills, max_input_tokens=100_000
    )


@pytest.fixture
def digest() -> str:
    return "\n".join(
        f"- iter {index}: writer drafted section {index} and closed {index % 3} issues"
        for index in range(40)
    )


@pytest.fixture
def state(brief: Brief, digest: str) -> FakeRunState:
    card = make_card()
    return FakeRunState(
        brief=brief,
        sections=(make_section(),),
        sources=(card,),
        digest=digest,
        texts={SECTION_ID: SECTION_TEXT},
    )


# --------------------------------------------------------------------------- layout
def test_fixed_layout_and_cache_flags(builder: ContextBuilder, state: FakeRunState):
    task = make_task()
    plan = builder.plan(task, state)

    stable = [block.text for block in plan.stable_blocks]
    assert stable[0].startswith("# Root rules")
    assert "Writer rules" in stable[1]
    assert stable[2].startswith("## Rubric excerpt")
    assert stable[3].startswith("## Skill index")
    assert stable[4].startswith("## Brief")
    assert all(block.cache for block in plan.stable_blocks)
    assert all(not block.cache for block in plan.semi_stable_blocks)

    request = plan.request
    assert request.system_blocks[: len(plan.stable_blocks)] == list(plan.stable_blocks)
    assert request.system_blocks[len(plan.stable_blocks) :] == list(plan.semi_stable_blocks)
    assert len(request.messages) == 1
    assert request.messages[0].role is MessageRole.USER
    assert request.max_output_tokens == 4_096


def test_build_returns_the_planned_request(builder: ContextBuilder, state: FakeRunState):
    task = make_task()

    request = builder.build(task, state, json_schema={"type": "object"}, max_output_tokens=512)

    assert request.json_schema == {"type": "object"}
    assert request.max_output_tokens == 512


def test_skill_index_is_name_plus_one_line(builder: ContextBuilder, state: FakeRunState):
    plan = builder.plan(make_task(), state)

    index = next(
        block.text for block in plan.stable_blocks if block.text.startswith("## Skill index")
    )
    lines = index.splitlines()[1:]
    assert lines, "the shipped skills directory must not be empty"
    assert all(line.startswith("- ") and ": " in line for line in lines)
    # Bodies never leak into the index: only selected skills may carry a body block.
    assert "## Skill: " not in index


def test_rubric_excerpt_includes_sections_and_brief_must_include(
    builder: ContextBuilder, state: FakeRunState
):
    plan = builder.plan(make_task(), state)

    excerpt = next(
        block.text for block in plan.stable_blocks if block.text.startswith("## Rubric excerpt")
    )
    assert "a-limitations-section" in excerpt
    assert "hard_gates:" in excerpt
    assert "criteria_weights:" in excerpt


def test_source_cards_are_limited_to_the_task_reference(
    builder: ContextBuilder, state: FakeRunState
):
    task = make_task(source_ids=[])
    plan = builder.plan(task, state)

    assert not any(block.text.startswith("## Source cards") for block in plan.semi_stable_blocks)


# --------------------------------------------------------------------------- prefix
def test_stable_prefix_is_byte_identical_across_tasks(builder: ContextBuilder, state: FakeRunState):
    first = builder.plan(make_task(seed=1), state)
    second = builder.plan(make_task(seed=9, section_ids=["introduction"]), state)

    assert [block.text for block in first.stable_blocks] == [
        block.text for block in second.stable_blocks
    ]
    assert first.prefix_hash == second.prefix_hash
    assert first.volatile_text != second.volatile_text


def test_stable_prefix_changes_with_the_role(builder: ContextBuilder, state: FakeRunState):
    writer = builder.plan(make_task(role=AgentRole.WRITER), state)
    reviewer = builder.plan(make_task(role=AgentRole.REVIEWER), state)

    assert writer.prefix_hash != reviewer.prefix_hash


def test_prefix_hash_matches_the_stable_text(builder: ContextBuilder, state: FakeRunState):
    plan = builder.plan(make_task(), state)

    assert plan.prefix_hash == content_hash(plan.stable_prefix)


# --------------------------------------------------------------------------- rules
def test_project_rules_are_appended_after_the_root_rules(
    repo_root: Path, rubric: Rubric, skills: SkillLoader, state: FakeRunState
):
    project = "- Project X: prefer en-GB spelling and British date formats."
    library = RuleLibrary(repo_root / "rules", project_rules=project)
    builder = ContextBuilder(rules=library, rubric=rubric, skills=skills, max_input_tokens=100_000)

    plan = builder.plan(make_task(), state)

    texts = [block.text for block in plan.stable_blocks]
    assert texts[0].startswith("# Root rules")
    assert texts[1] == project
    assert "Writer rules" in texts[2]


def test_project_rules_may_not_remove_a_mandatory_rule(
    repo_root: Path, rubric: Rubric, skills: SkillLoader, state: FakeRunState
):
    library = RuleLibrary(repo_root / "rules", project_rules="- remove: Never fabricate sources")
    builder = ContextBuilder(rules=library, rubric=rubric, skills=skills, max_input_tokens=100_000)

    with pytest.raises(MandatoryRuleRemoved) as caught:
        builder.plan(make_task(), state)

    assert "remove" in caught.value.message


def test_guard_rejects_a_composition_that_lost_a_rule():
    root = "# Rules\n\n1. Never fabricate sources.\n2. Never log a key.\n"
    guard = MandatoryRuleGuard.from_root(root)
    assert len(guard.mandatory) == 2

    with pytest.raises(MandatoryRuleRemoved) as caught:
        guard.check(project_text="", composed_text="# Rules\n\n1. Never fabricate sources.\n")
    assert caught.value.context["missing_count"] == 1

    guard.check(project_text="- added project rule", composed_text=root)


def test_root_rule_items_are_all_present_in_the_prefix(
    builder: ContextBuilder, state: FakeRunState, rule_library: RuleLibrary
):
    bundle = rule_library.load(AgentRole.WRITER)
    plan = builder.plan(make_task(), state)

    assert bundle.mandatory
    for rule in bundle.mandatory:
        assert " ".join(rule.split()) in " ".join(plan.stable_prefix.split())


def test_mandatory_rule_extraction_skips_prose():
    rules = mandatory_rules("# Title\n\nProse line.\n\n1. First rule.\n- Second rule.\n")
    assert rules == ("First rule.", "Second rule.")


def test_a_bundle_without_root_rules_is_rejected():
    with pytest.raises(ValueError, match="root rules"):
        RuleBundle(root="   ")


def test_missing_rules_files_are_a_config_error(
    rubric: Rubric, skills: SkillLoader, tmp_path: Path
):
    library = RuleLibrary(tmp_path)
    builder = ContextBuilder(rules=library, rubric=rubric, skills=skills, max_input_tokens=100_000)

    with pytest.raises(ConfigError, match="rules file not found"):
        builder.plan(make_task(), FakeRunState(brief=Brief.model_validate(SAMPLE_BRIEF)))


# --------------------------------------------------------------------------- volatile
def test_section_text_and_task_travel_verbatim(builder: ContextBuilder, state: FakeRunState):
    task = make_task()
    plan = builder.plan(task, state)

    assert plan.volatile_text.startswith(TASK_INSTRUCTION)
    assert _task_json(task) in plan.volatile_text
    assert SECTION_TEXT in plan.volatile_text
    assert "version 2" in plan.volatile_text


def test_section_texts_mapping_is_also_supported(builder: ContextBuilder, brief: Brief):
    state = MappingOnlyRunState(
        brief=brief,
        sections=(make_section(),),
        sources=(make_card(),),
        section_texts={SECTION_ID: SECTION_TEXT},
    )

    plan = builder.plan(make_task(), state)

    assert SECTION_TEXT in plan.volatile_text


def test_missing_section_text_is_stated_not_invented(builder: ContextBuilder, state: FakeRunState):
    empty = FakeRunState(brief=state.brief, sections=(make_section(),), texts={})

    plan = builder.plan(make_task(), empty)

    assert "(no text yet" in plan.volatile_text


# --------------------------------------------------------------------------- trims
def test_digest_is_trimmed_first_and_keeps_the_newest_detail(
    builder: ContextBuilder, state: FakeRunState
):
    task = make_task()
    baseline = builder.plan(task, state)
    digest_tokens = estimate_tokens(state.digest)

    plan = builder.plan(task, state, max_input_tokens=baseline.input_tokens - 5)

    assert [action.step for action in plan.trims] == [TrimStep.DIGEST_DETAIL]
    assert 0 < plan.trims[0].tokens_freed <= digest_tokens
    assert plan.input_tokens <= plan.max_input_tokens
    assert "newest detail retained" in plan.volatile_text
    assert state.digest.splitlines()[-1] in plan.volatile_text
    assert state.digest.splitlines()[0] not in plan.volatile_text
    # Nothing else moved: the stable prefix and the skill bodies are untouched.
    assert [block.text for block in plan.stable_blocks] == [
        block.text for block in baseline.stable_blocks
    ]
    assert plan.skills == baseline.skills


def test_trim_order_is_monotone_and_never_cuts_the_task(
    builder: ContextBuilder, state: FakeRunState
):
    task = make_task()
    baseline = builder.plan(task, state)
    assert baseline.skills, "the fixture task must select at least one skill"

    seen: set[TrimStep] = set()
    previous: tuple[TrimStep, ...] = ()
    for limit in range(baseline.input_tokens, 1_000, -25):
        try:
            plan = builder.plan(task, state, max_input_tokens=limit)
        except ContextTooLarge:
            break
        steps = tuple(action.step for action in plan.trims)
        assert steps == tuple(sorted(steps, key=TRIM_ORDER.index)), (
            f"trim order violated at limit {limit}: {steps}"
        )
        # Lowering the ceiling never un-trims a tier: the set of applied steps grows.
        assert set(previous) <= set(steps), f"trim tier regressed at limit {limit}: {steps}"
        previous = steps
        seen.update(steps)

        # The invariants that hold at every limit.
        assert plan.input_tokens <= limit
        assert plan.volatile_text.startswith(TASK_INSTRUCTION)
        assert _task_json(task) in plan.volatile_text
        assert SECTION_TEXT in plan.volatile_text
        assert [block.text for block in plan.stable_blocks] == [
            block.text for block in baseline.stable_blocks
        ]

    assert seen == set(TRIM_ORDER), f"every trim step must be reachable, saw {seen}"


def test_skill_bodies_go_but_the_index_stays(builder: ContextBuilder, state: FakeRunState):
    task = make_task()
    baseline = builder.plan(task, state)
    limit = baseline.input_tokens - estimate_tokens(state.digest) - 200

    plan = builder.plan(task, state, max_input_tokens=limit)

    assert TrimStep.OPTIONAL_SKILLS in [action.step for action in plan.trims]
    assert len(plan.skills) < len(baseline.skills)
    assert any(block.text.startswith("## Skill index") for block in plan.stable_blocks)
    assert plan.prefix_hash == baseline.prefix_hash


def test_source_card_notes_are_dropped_before_the_call_is_refused(
    builder: ContextBuilder, state: FakeRunState
):
    task = make_task()
    baseline = builder.plan(task, state)
    trimmed_plan = None
    for limit in range(baseline.input_tokens, 1_000, -10):
        try:
            candidate = builder.plan(task, state, max_input_tokens=limit)
        except ContextTooLarge:
            break
        if TrimStep.SOURCE_CARD_NOTES in [action.step for action in candidate.trims]:
            trimmed_plan = candidate
            break

    assert trimmed_plan is not None
    cards = [
        block.text
        for block in trimmed_plan.semi_stable_blocks
        if block.text.startswith("## Source cards")
    ]
    assert cards, "cards stay: only their notes are optional"
    assert '"note"' not in cards[0]
    assert make_card().id in cards[0]


def test_json_schema_tokens_count_against_the_ceiling(builder: ContextBuilder, state: FakeRunState):
    task = make_task()
    without = builder.plan(task, state)
    schema = {"type": "object", "description": "x" * 4_000}

    with_schema = builder.plan(task, state, json_schema=schema)

    assert with_schema.schema_tokens > 0
    assert with_schema.input_tokens > without.input_tokens

    # Held to the schema-free ceiling, the builder makes room by trimming (the schema
    # itself is never dropped - it is what makes the answer parseable).
    squeezed = builder.plan(task, state, json_schema=schema, max_input_tokens=without.input_tokens)
    assert squeezed.trimmed
    assert squeezed.input_tokens <= squeezed.max_input_tokens
    assert squeezed.schema_tokens == with_schema.schema_tokens


def test_input_tokens_match_the_providers_own_count(builder: ContextBuilder, state: FakeRunState):
    """The builder's ceiling and the governor's precheck must measure the same prompt."""
    task = make_task()

    plain = builder.plan(task, state)
    structured = builder.plan(task, state, json_schema={"type": "object"})

    assert plain.input_tokens == prompt_tokens(plain.request) + plain.schema_tokens
    assert structured.input_tokens == prompt_tokens(structured.request) + structured.schema_tokens


def test_a_repeated_source_id_renders_one_card(builder: ContextBuilder, state: FakeRunState):
    card = make_card()
    task = make_task(source_ids=[card.id, card.id])

    plan = builder.plan(task, state)

    cards = [
        block.text for block in plan.semi_stable_blocks if block.text.startswith("## Source cards")
    ]
    assert len(cards) == 1
    assert len(cards[0].splitlines()) == 2  # header + exactly one card line


def test_an_oversized_section_is_refused_not_a_contract_error(
    builder: ContextBuilder, state: FakeRunState
):
    """A message longer than LLMMessage allows must raise ContextTooLarge, not pydantic."""
    huge = "word " * 100_020
    state_with_huge_text = FakeRunState(
        brief=state.brief,
        sections=(make_section(),),
        sources=(make_card(),),
        texts={SECTION_ID: huge},
    )

    with pytest.raises(ContextTooLarge) as caught:
        builder.plan(make_task(), state_with_huge_text, max_input_tokens=200_000)

    assert caught.value.context["max_message_chars"] == 500_000


def test_output_ceiling_is_validated_before_the_request(
    builder: ContextBuilder, state: FakeRunState
):
    with pytest.raises(ValueError, match="max_output_tokens"):
        builder.plan(make_task(), state, max_output_tokens=100_000)


def test_context_too_large_is_refused_rather_than_truncated(
    builder: ContextBuilder, state: FakeRunState
):
    with pytest.raises(ContextTooLarge) as caught:
        builder.plan(make_task(), state, max_input_tokens=200)

    context = caught.value.context
    assert context["max_input_tokens"] == 200
    assert context["volatile_tokens"] > 0
    assert context["role"] == "writer"


def test_ceilings_must_be_positive(builder: ContextBuilder, state: FakeRunState):
    with pytest.raises(ValueError, match="ceilings"):
        builder.plan(make_task(), state, max_input_tokens=0)


def test_builder_rejects_a_useless_ceiling(rubric: Rubric, skills: SkillLoader):
    with pytest.raises(ValueError, match="max_input_tokens"):
        ContextBuilder(
            rules=RuleBundle(root="# Rules\n\n1. Be honest.\n"),
            rubric=rubric,
            skills=skills,
            max_input_tokens=0,
        )
