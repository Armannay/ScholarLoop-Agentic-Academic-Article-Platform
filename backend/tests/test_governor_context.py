"""Context assembly: stable prefix, mandatory rules, trim order, hard ceilings.

Three properties are load-bearing and tested here:

1. **Cache friendliness** - the stable prefix is byte-identical for every call of
   the same role within a run, so the provider can serve it from cache.
2. **Rule integrity** - project rules may add to root rules but never remove a
   mandatory one (``## Never`` / ``[mandatory]``).
3. **No silent loss** - when a prompt is too large, the documented trims happen
   (digest, then optional skills, then card notes) and the task and section text
   survive verbatim; otherwise ``ContextTooLarge`` is raised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.enums import BindingRole
from app.contracts.patch import Patch  # noqa: F401  (keeps the contracts import path warm)
from app.contracts.task import Task
from app.core.policies import Policies
from app.governor.context_builder import (
    ContextBuilder,
    ContextTooLarge,
    MandatoryRuleViolation,
    RunState,
    SkillInfo,
    SkillRef,
    build,
    mandatory_rules,
    merge_rules,
    validate_mandatory_rules,
)
from app.providers.base import prompt_tokens
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.config

ROOT_RULES = """# Root rules
## Never
1. Never fabricate sources, data, quotes or numbers. Unknown -> needs_user_input.
2. Never print, log or store an API key.
## Always
- Pass references, not content.
"""

PROJECT_RULES_ADDITIVE = "## Project\n- Prefer primary sources. The venue is ACM CHI."

TASK_ID = sample_ulid(0)


def make_task(**overrides: object) -> Task:
    """A writer task over one section."""
    values: dict[str, object] = {
        "id": TASK_ID,
        "title": "Draft the introduction",
        "role": "writer",
        "section_ids": ["introduction"],
        "source_ids": [],
        "instructions": "Open with the gap, then state the contribution.",
        "acceptance": ["No unsupported claims", "Ends with the contribution"],
        "token_budget": 6_000,
        "depends_on": [],
    }
    values.update(overrides)
    return Task(**values)


def make_state(**overrides: object) -> RunState:
    """A complete run state with small, deterministic contents."""
    values: dict[str, object] = {
        "run_id": "run-42",
        "iteration": 1,
        "role": "writer",
        "brief": Brief(**SAMPLE_BRIEF),
        "root_rules": ROOT_RULES,
        "project_rules": PROJECT_RULES_ADDITIVE,
        "role_rules": "## Writer\n- Prefer small patches.",
        "rubric_excerpt": "Gates: required_sections_present, no_placeholders",
        "skills": [SkillRef("search", "Metadata lookup over Crossref")],
        "selected_skills": [],
        "source_cards": [],
        "digest": "Iter 0: architecture drafted\n---\nIter 1: sources collected",
        "section_text": "THE SECTION TEXT BEING EDITED.",
        "policies": Policies(),
    }
    values.update(overrides)
    return RunState(**values)


def user_message(request) -> str:
    """The volatile layer (the final user message)."""
    return request.messages[-1].text


# ------------------------------------------------------------------ stable prefix
def test_stable_prefix_layers_appear_in_the_documented_order() -> None:
    """Rules -> rubric -> skill index -> brief, all inside the cacheable block."""
    request = build(make_task(), make_state())
    prefix = request.system_blocks[0].text

    positions = [
        prefix.index("Rules:"),
        prefix.index("Never fabricate sources"),
        prefix.index("## Project"),
        prefix.index("## Writer"),
        prefix.index("Rubric:"),
        prefix.index("Skills index"),
        prefix.index("Brief:"),
    ]
    assert positions == sorted(positions)
    assert "search: Metadata lookup over Crossref" in prefix


def test_stable_prefix_is_byte_identical_across_iterations() -> None:
    """The whole point of the layout: same role, same run, same prefix bytes."""
    first = build(make_task(), make_state(iteration=1, digest="digest v1"))
    second = build(
        make_task(),
        make_state(
            iteration=7,
            digest="a completely different digest after compaction",
            section_text="A rewritten section with new words.",
        ),
    )

    assert first.system_blocks[0].text == second.system_blocks[0].text
    assert first.system_blocks[0].cache is True
    assert second.system_blocks[0].cache is True


def test_stable_prefix_changes_with_role_but_not_with_task() -> None:
    """Different role rules => different prefix; a different task does not."""
    writer = build(make_task(), make_state(role="writer"))
    reviewer = build(
        make_task(role="reviewer"),
        make_state(role="reviewer", role_rules="## Reviewer\n- Judge against acceptance."),
    )
    other_task = build(make_task(title="Draft the discussion"), make_state())

    assert writer.system_blocks[0].text != reviewer.system_blocks[0].text
    assert "## Writer" in writer.system_blocks[0].text
    assert "## Reviewer" in reviewer.system_blocks[0].text
    assert writer.system_blocks[0].text == other_task.system_blocks[0].text


def test_digest_and_section_text_never_leak_into_the_prefix() -> None:
    """Volatile data after the cache boundary, or the cache never hits."""
    request = build(make_task(), make_state())

    prefix = request.system_blocks[0].text
    assert "digest v1" not in prefix
    assert "THE SECTION TEXT" not in prefix
    assert "digest v1" not in prefix
    assert "THE SECTION TEXT" in user_message(request)


def test_task_and_section_text_survive_verbatim() -> None:
    """HARD RULE 1 depends on the writer seeing exactly what it was asked."""
    task = make_task()
    state = make_state()
    request = build(task, state)
    user = user_message(request)

    assert task.instructions in user
    assert state.section_text in user
    for criterion in task.acceptance:
        assert criterion in user
    assert f"Token budget: {task.token_budget}" in user, "the agent must know its ceiling"
    assert "Acceptance:" in user


def test_only_source_cards_the_task_references_are_sent() -> None:
    """The semi-stable layer is "cards the task references", not the whole store."""
    referenced = sample_ulid(1)
    other = sample_ulid(2)
    cards = [
        {"id": other, "title": "UNREFERENCED CARD", "authors": ["B"], "year": 2020, "snippet": "b"},
        {"id": referenced, "title": "Referenced", "authors": ["A"], "year": 2021, "snippet": "a"},
    ]
    request = build(
        make_task(source_ids=[referenced]),
        make_state(source_cards=cards),
    )
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    assert referenced in prompt and "Referenced" in prompt and "2021" in prompt
    assert other not in prompt, "an unreferenced card is prompt cost with no use"
    assert "UNREFERENCED CARD" not in prompt


def test_a_task_without_source_ids_gets_no_cards() -> None:
    """No references, no card payload: the researcher task is the one that reads sources."""
    cards = [
        {
            "id": sample_ulid(9),
            "title": "CARD",
            "authors": ["A"],
            "year": 2020,
            "snippet": "x",
        }
    ]
    request = build(make_task(source_ids=[]), make_state(source_cards=cards))
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    assert "CARD" not in prompt


def test_output_cap_comes_from_the_binding() -> None:
    """The binding's max_output_tokens is the request's cap."""
    binding = AgentBinding(
        role=BindingRole.WRITER,
        account_ids=[sample_ulid(3), sample_ulid(4)],
        strategy="failover",
        model="mock-model",
        max_input_tokens=24_000,
        max_output_tokens=1_234,
    )
    request = build(make_task(), make_state(), binding=binding, max_input_tokens=24_000)

    assert request.max_output_tokens == 1_234


def test_builder_does_not_mutate_the_run_state_it_is_given() -> None:
    """State is shared across tasks: a builder default must not be written into it."""
    state = RunState(run_id="run-mut", role="writer")
    builder = ContextBuilder(root_rules=ROOT_RULES, rubric_excerpt="Gates: all eight")

    builder.build(make_task(), state)

    assert state.root_rules is None, "the caller's state must come back untouched"
    assert state.rubric_excerpt is None


def test_output_cap_never_exceeds_the_task_budget() -> None:
    """Asking for more output than the task may spend guarantees a silent overrun."""
    task = make_task(token_budget=600)
    request = build(task, make_state())

    assert request.max_output_tokens <= task.token_budget


def test_builder_caches_rules_and_prefixes_across_calls(repo_root: Path) -> None:
    """The orchestrator reads rules once; the builder keeps the prefix stable."""
    root_text = (repo_root / "rules" / "root.rules.md").read_text(encoding="utf-8")
    builder = ContextBuilder(
        root_rules=root_text,
        rubric_excerpt="Gates: all eight",
        skill_index=[SkillRef("search", "Crossref metadata")],
    )

    first = builder.build(make_task(), RunState(run_id="run-b", role="writer"))
    second = builder.build(
        make_task(),
        RunState(run_id="run-b", role="writer", digest="iteration two"),
    )

    assert first.system_blocks[0].text == second.system_blocks[0].text
    assert builder.stable_prefix(make_task(), RunState(run_id="run-b", role="writer")) == (
        first.system_blocks[0].text
    )


# ------------------------------------------------------------- mandatory rules
def test_mandatory_rules_are_read_from_the_never_section() -> None:
    """The shipped convention: bullets under ``## Never`` are mandatory."""
    rules = mandatory_rules(ROOT_RULES)

    assert rules == [
        "Never fabricate sources, data, quotes or numbers. Unknown -> needs_user_input.",
        "Never print, log or store an API key.",
    ]


def test_mandatory_rules_can_also_be_tagged() -> None:
    """An explicit ``[mandatory]`` marker wins over the heading heuristic."""
    rules = mandatory_rules("## Style\n- Be terse. [mandatory]\n## Never\n1. Never do X.\n")

    assert rules == ["Be terse."]


def test_additive_project_rules_are_allowed_and_merged() -> None:
    """Adding guidance never threatens a mandatory rule."""
    merged = merge_rules(ROOT_RULES, PROJECT_RULES_ADDITIVE, "## Writer\n- Small patches.")

    assert merged.index("Never fabricate") < merged.index("## Project")
    assert merged.index("## Project") < merged.index("## Writer")
    assert "Prefer primary sources" in merged


def test_project_rules_cannot_drop_a_mandatory_rule() -> None:
    """A project-level Never block that omits "never fabricate" is rejected."""
    replacement = "## Never\n1. Never print or store an API key.\n"

    with pytest.raises(MandatoryRuleViolation) as excinfo:
        build(make_task(), make_state(project_rules=replacement))

    assert "Never fabricate" in excinfo.value.message
    assert excinfo.value.code == "mandatory_rule_violation"


def test_project_rules_may_restate_and_extend_the_never_block() -> None:
    """Restating every mandatory rule keeps the invariant; additions are free."""
    replacement = (
        "## Never\n"
        "1. Never fabricate sources, data, quotes or numbers.\n"
        "2. Never print, log or store an API key.\n"
        "3. Never cite a retracted paper.\n"
    )
    request = build(make_task(), make_state(project_rules=replacement))

    assert "Never cite a retracted paper" in request.system_blocks[0].text


def test_root_mandatory_rule_is_always_present_in_the_prefix() -> None:
    """Even a compliant project block cannot erase the root text."""
    request = build(make_task(), make_state())

    assert "Never fabricate sources" in request.system_blocks[0].text


def test_validation_helper_is_callable_on_its_own() -> None:
    """Other modules may pre-validate project rules before building a request."""
    validate_mandatory_rules(ROOT_RULES, PROJECT_RULES_ADDITIVE)


# ------------------------------------------------------------------- trim order
def test_digest_is_trimmed_first_and_skills_survive() -> None:
    """Overflow caused by the digest must eat the digest, not the instructions."""
    skill = SkillInfo(name="search", body="SKILL BODY MARKER " * 20, optional=True)
    state = make_state(
        digest="OLDEST ENTRY\n---\n" + ("digest detail " * 2_000),
        selected_skills=[skill],
    )

    request = build(make_task(), state, max_input_tokens=2_000)
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    assert "SKILL BODY MARKER" in prompt, "trim 1 must not touch optional skills"
    assert "OLDEST ENTRY" not in prompt, "the oldest digest detail is dropped first"
    assert "THE SECTION TEXT BEING EDITED." in prompt
    assert make_task().instructions in prompt
    assert prompt_tokens(request) <= 2_000


def test_optional_skills_are_dropped_before_source_card_notes() -> None:
    """Trim 2 removes optional skill bodies and leaves the cards intact."""
    skill = SkillInfo(name="style", body="OPTIONAL SKILL " * 1_500, optional=True)
    card_id = sample_ulid(5)
    cards = [
        {
            "id": card_id,
            "title": "CARD TITLE",
            "authors": ["A"],
            "year": 2020,
            "snippet": "short note",
        }
    ]
    state = make_state(digest="tiny", selected_skills=[skill], source_cards=cards)

    request = build(make_task(source_ids=[card_id]), state, max_input_tokens=2_000)
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    assert "OPTIONAL SKILL" not in prompt
    assert "CARD TITLE" in prompt
    assert "THE SECTION TEXT BEING EDITED." in prompt
    assert prompt_tokens(request) <= 2_000


def test_mandatory_skills_are_never_dropped() -> None:
    """Only *optional* skill bodies are trim 2's business."""
    skill = SkillInfo(name="safety", body="MANDATORY SKILL RULE", optional=False)
    state = make_state(digest="tiny", selected_skills=[skill])

    request = build(make_task(), state, max_input_tokens=2_000)
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    assert "MANDATORY SKILL RULE" in prompt


def test_source_card_notes_are_shortened_last() -> None:
    """Trim 3 shortens snippets; the card ids stay so citations still resolve."""
    card_ids = [sample_ulid(index) for index in range(5, 8)]
    cards = [
        {
            "id": card_id,
            "title": f"Card {index}",
            "authors": ["Author"],
            "year": 2020,
            "snippet": "A finding that matters. " * 1_000,
        }
        for index, card_id in zip(range(5, 8), card_ids, strict=True)
    ]
    state = make_state(digest="tiny", source_cards=cards)

    request = build(make_task(source_ids=card_ids), state, max_input_tokens=2_000)
    prompt = "\n".join(block.text for block in request.system_blocks) + user_message(request)

    for card in cards:
        assert card["id"] in prompt, "the citation handle must survive trimming"
    assert prompt_tokens(request) <= 2_000
    assert "THE SECTION TEXT BEING EDITED." in prompt


def test_task_and_section_are_never_silently_truncated() -> None:
    """Nothing that changes what the agent must do may be cut."""
    long_section = "SECTION SENTENCE. " * 40
    state = make_state(digest="detail " * 5_000, section_text=long_section)

    request = build(make_task(), state, max_input_tokens=2_000)
    user = user_message(request)

    assert long_section.strip() in user
    assert make_task().instructions in user


def test_context_too_large_is_raised_when_the_section_alone_cannot_fit() -> None:
    """Better to stop than to hand the writer half an assignment."""
    state = make_state(digest="", section_text="UNBREAKABLE SECTION " * 2_000)

    with pytest.raises(ContextTooLarge) as excinfo:
        build(make_task(), state, max_input_tokens=1_000)

    error = excinfo.value
    assert error.code == "context_too_large"
    assert error.context["ceiling"] == 1_000
    assert error.context["tokens"] > 1_000


def test_ceiling_falls_back_to_policies_when_not_passed_explicitly() -> None:
    """The policies' call_input_cap is the default ceiling."""
    tight = Policies(
        run_token_cap=10_000,
        iteration_token_cap=5_000,
        call_input_cap=1_000,
        digest_max_tokens=500,
    )
    state = make_state(policies=tight, section_text="SECTION " * 5_000)

    with pytest.raises(ContextTooLarge):
        build(make_task(), state)


def test_request_stays_under_the_default_ceiling_for_a_normal_case() -> None:
    """The happy path is comfortably inside the shipped 24k ceiling."""
    request = build(make_task(), make_state())

    assert prompt_tokens(request) < 24_000
    assert request.max_output_tokens <= 64_000
