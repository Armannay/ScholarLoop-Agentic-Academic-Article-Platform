"""Step 7 - the five role agents, end to end on the Step 5 runtime.

Every test drives a real role agent (real context builder, real rules and prompt assets,
real governor, real tool registry for the validator) against a ``MockProvider`` scripted
from ``prompts/<role>.golden.json``. Nothing here reaches the network.

The suite is organised around the five claims the role briefs make:

* **The fixtures are valid answers.** One test per role proves the golden answer validates
  against both documents the runtime accepts, so the example a reader copies is a legal
  answer and not merely a plausible one.
* **Each role's acceptance checks are the difference between plausible and correct.**
  Unknown citation ids, an invented source, a reviewer's rewritten paragraph, a writer
  that ignores its word window - each is refused, and refused *once*: the role gets the
  same single repair call a schema error gets.
* **The validator's hard gates are the tools' output**, never the model's opinion: the
  model is not even offered a gate field, and a model answer that claims everything is
  fine still yields a failing gate when a placeholder is present.
* **Q comes from the reviewer and is carried forward only for unchanged text.**
* **Token caps are ceilings over the binding**, in both directions (output clamp and a
  local refusal before an oversized prompt is sent).
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.errors import AgentOutputInvalid, AgentTaskMismatch
from app.agents.roles.errors import RoleTaskInvalid
from app.agents.roles.factory import ROLE_AGENTS
from app.agents.roles.golden import load_golden, load_goldens
from app.agents.roles.inputs import (
    MappingSectionTargets,
    ReviewScoreboard,
    StaticReportSource,
)
from app.agents.roles.quality import (
    BASELINE_SCORE,
    figure_and_table_gate,
    merge_scores,
    weighted_q,
)
from app.agents.roles.schemas import (
    MAX_FINDINGS,
    MAX_ISSUES_PER_SECTION,
    MAX_SUGGESTED_FIX_WORDS,
    ResearcherOutput,
    ReviewerOutput,
)
from app.agents.roles.validation import cite_marker_ids, estimated_words, matches_known_source
from app.agents.roles.writer import WORD_TOLERANCE as WRITER_WORD_TOLERANCE
from app.contracts.agent import AgentResult
from app.contracts.enums import (
    AgentRole,
    BindingRole,
    ChangeStatus,
    PatchOpType,
    SectionStatus,
)
from app.contracts.patch import Patch
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.core.errors import BudgetExceeded, ConfigError
from app.core.ids import new_ulid
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.providers.base import estimate_tokens
from tests.role_support import (
    RecordingProvider,
    agent_binding,
    build_agent,
    governor,
    mutated_answer,
    researcher_tools,
    scripted_provider,
    validator_tools,
)

pytestmark = pytest.mark.agents

#: Every role, in enum order: the suite parametrises over all five wherever it can.
ROLES: tuple[AgentRole, ...] = tuple(AgentRole)
#: The introduction fixture's planned length (the writer's golden task).
INTRODUCTION_TARGET = 120


# ----------------------------------------------------------------------------- helpers
def run_state_for(role: AgentRole, repo_root: Path) -> Any:
    """The golden run state for ``role``."""
    return load_golden(role, root=repo_root).run_state()


def tools_for(role: AgentRole, rubric: Rubric, case: Any) -> Any:
    """The tools a role needs in an end-to-end test, and no others."""
    if role is AgentRole.RESEARCHER:
        return researcher_tools(case.expected.result["source_cards"])
    if role is AgentRole.VALIDATOR:
        return validator_tools(rubric)
    return None


async def run_golden(
    role: AgentRole,
    *,
    repo_root: Path,
    policies: Policies,
    rubric: Rubric,
    answer: str | None = None,
    task: Task | None = None,
    run_state: Any = None,
    tools: Any = None,
    binding: Any = None,
    scoreboard: ReviewScoreboard | None = None,
    reports: Any = None,
    prompt: str | None = None,
) -> tuple[AgentResult, Any, RecordingProvider]:
    """Run ``role`` over its fixture (or a mutation of it) and return everything."""
    case = load_golden(role, root=repo_root)
    script = answer if answer is not None else case.answer_json()
    provider = RecordingProvider(scripted_provider(role, script, script))
    agent = build_agent(
        role,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        binding=binding,
        tools=tools if tools is not None else tools_for(role, rubric, case),
        section_texts=case.section_texts(),
        scoreboard=scoreboard,
        reports=reports,
        targets=MappingSectionTargets(
            {section.id: INTRODUCTION_TARGET for section in case.input.sections}
        )
        if role is AgentRole.WRITER
        else None,
        prompt=prompt,
    )
    result = await agent.run(
        task if task is not None else case.input.task,
        run_state if run_state is not None else case.run_state(),
    )
    return result, agent, provider


def section_texts_of(case: Any) -> dict[str, str]:
    """Section id -> body text, from a golden fixture."""
    return {entry.id: entry.text for entry in case.input.texts}


def drafted(section_id: str, *, title: str = "Drafted section") -> SectionState:
    """A section that already carries text, for re-plan and edit-after-review tests."""
    return SectionState(
        id=section_id,
        title=title,
        order=0,
        version=1,
        hash="sha256:" + "ab" * 32,
        words=130,
        status=SectionStatus.DRAFT,
    )


# --------------------------------------------------------------- fixtures and assets
def test_every_role_has_a_registered_agent() -> None:
    """The dispatcher table is the only wiring authority: all five roles, no strays."""
    assert set(ROLE_AGENTS) == set(AgentRole)
    for role, agent_class in ROLE_AGENTS.items():
        assert agent_class.role is role
        assert agent_class.output_model is not None
        assert agent_class.max_output_tokens > 0


def test_every_golden_fixture_loads(repo_root: Path) -> None:
    """One fixture per role, all parseable, each declaring the role it belongs to."""
    cases = load_goldens(root=repo_root)
    assert set(cases) == set(AgentRole)


@pytest.mark.parametrize("role", ROLES)
def test_golden_expected_matches_the_role_schemas(role: AgentRole, repo_root: Path) -> None:
    """The fixture's answer validates as both the envelope and the role document."""
    case = load_golden(role, root=repo_root)
    assert case.role is role
    ROLE_AGENTS[role].output_model.model_validate(case.expected.output)
    AgentResult.model_validate(case.expected.result)


@pytest.mark.parametrize("role", ROLES)
def test_call_schema_is_the_envelope_plus_the_role_document(
    role: AgentRole, repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The constrained output has two required parts, and no forbidden field at all."""
    agent = build_agent(
        role,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=RecordingProvider(scripted_provider(role)),
    )
    schema = agent.call_schema()
    agent_class = ROLE_AGENTS[role]
    assert schema["required"] == ["result", "output"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["output"]["type"] == "object"
    result_properties = schema["properties"]["result"]["properties"]
    assert result_properties, "the envelope half of the schema is missing"
    for field in agent_class.forbidden_result_fields:
        assert field not in result_properties, f"{role.value} may not be offered result.{field}"
    for reference in _refs(schema):
        assert reference.rsplit("/", 1)[-1] in schema["$defs"], f"unresolved $ref {reference}"


def _refs(node: Any) -> list[str]:
    """Every ``$ref`` string inside a JSON schema."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


@pytest.mark.parametrize("role", ROLES)
def test_prompt_asset_stays_within_its_budget(role: AgentRole, repo_root: Path) -> None:
    """A prompt is re-sent on every call: same 400-token budget as the rules file."""
    text = (repo_root / "prompts" / f"{role.value}.md").read_text(encoding="utf-8")
    tokens = estimate_tokens(text)
    assert tokens <= 400, f"prompts/{role.value}.md is {tokens} tokens (budget 400)"


@pytest.mark.parametrize("role", ROLES)
def test_role_metadata_matches_the_configuration(
    role: AgentRole, repo_root: Path, policies: Policies
) -> None:
    """Tier and tool grants are configuration, so the configuration is what pins them."""
    import yaml

    agent_class = ROLE_AGENTS[role]
    assert agent_class.default_tier is policies.tier_for(BindingRole(role.value))

    policy = yaml.safe_load((repo_root / "config" / "tool_policy.yaml").read_text(encoding="utf-8"))
    granted = set(policy.get("roles", {}).get(role.value, []))
    declared = set(agent_class.allowed_tools)
    assert declared <= granted, (
        f"{role.value} declares tools the policy does not grant: {sorted(declared - granted)}"
    )

    assert 0 < agent_class.max_output_tokens <= 64_000
    assert 0 < agent_class.max_input_tokens <= policies.call_input_cap


# ----------------------------------------------------------------------- end to end
@pytest.mark.parametrize("role", ROLES)
async def test_role_answers_its_golden_fixture(
    role: AgentRole, repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Each role produces exactly the contract-shaped answer its fixture describes."""
    case = load_golden(role, root=repo_root)
    result, agent, provider = await run_golden(
        role, repo_root=repo_root, policies=policies, rubric=rubric
    )
    expected = ROLE_AGENTS[role].output_model.model_validate(case.expected.output)

    assert result.entry.role is role
    assert result.entry.task_id == case.input.task.id
    assert result.entry.status is ChangeStatus.OK
    assert provider.call_count == 1
    assert agent.take_output(case.input.task) is None  # scratch is per run, then dropped

    if role is AgentRole.ARCHITECT:
        assert [state.id for state in result.sections] == expected.ordered_ids
        assert all(state.status is SectionStatus.EMPTY for state in result.sections)
        assert result.patches == []
        assert result.report is None
    elif role is AgentRole.RESEARCHER:
        assert {card.id for card in result.source_cards} == {
            finding.source_id for finding in expected.findings
        }
        assert result.patches == []
        assert result.report is None
    elif role is AgentRole.WRITER:
        assert len(result.patches) == 1
        op = result.patches[0].ops[0]
        assert op.op is PatchOpType.REPLACE_SECTION
        assert result.entry.patch_ref == f"patch:{result.patches[0].patch_id}"
        markers = cite_marker_ids(op.text or "")
        assert sorted(markers) == sorted(expected.cited_source_ids)
    elif role is AgentRole.REVIEWER:
        assert [state.status for state in result.sections] == [SectionStatus.REVIEWED]
        assert result.patches == []
        assert result.report is None
    else:
        assert result.report is not None
        assert set(result.report.gates) == set(GATE_NAMES)
        assert result.patches == []


@pytest.mark.parametrize("role", ROLES)
async def test_a_role_refuses_another_roles_task(
    role: AgentRole, repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Scope is checked before a token is spent: cross-role dispatch is a bug."""
    case = load_golden(role, root=repo_root)
    other = AgentRole.WRITER if role is not AgentRole.WRITER else AgentRole.REVIEWER
    task = case.input.task.model_copy(update={"role": other})
    provider = RecordingProvider(scripted_provider(role, case.answer_json()))
    agent = build_agent(
        role,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        section_texts=case.section_texts(),
    )
    with pytest.raises(AgentTaskMismatch):
        await agent.run(task, case.run_state())
    assert provider.call_count == 0


# ------------------------------------------------------------------------- token caps
@pytest.mark.parametrize("role", ROLES)
async def test_role_output_cap_is_a_ceiling_over_the_binding(
    role: AgentRole, repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A generous binding cannot make a role ask for a longer answer than its own cap."""
    agent_class = ROLE_AGENTS[role]
    binding = agent_binding(role, max_input_tokens=64_000, max_output_tokens=64_000)
    _result, _agent, provider = await run_golden(
        role, repo_root=repo_root, policies=policies, rubric=rubric, binding=binding
    )
    assert provider.requests[0].max_output_tokens == agent_class.max_output_tokens


@pytest.mark.parametrize("role", ROLES)
async def test_role_refuses_a_prompt_over_its_input_cap(
    role: AgentRole, repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The cap is enforced locally: no provider call is made for an oversized prompt."""
    case = load_golden(role, root=repo_root)
    provider = RecordingProvider(scripted_provider(role, case.answer_json()))
    agent = build_agent(
        role,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        tools=tools_for(role, rubric, case),
        section_texts=case.section_texts(),
        prompt="padding sentence " * 11_000,  # ~47k tokens
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await agent.run(case.input.task, case.run_state())
    assert "role ceiling" in str(excinfo.value)
    assert provider.call_count == 0


# -------------------------------------------------------------------------- architect
async def test_architect_rejects_an_id_the_rubric_does_not_know(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A structure the rubric cannot express is a planning error, not a preference."""
    case = load_golden(AgentRole.ARCHITECT, root=repo_root)
    output = deepcopy(case.expected.output)
    output["sections"][0]["id"] = "opening-remarks"
    result = deepcopy(case.expected.result)
    result["sections"][0]["id"] = "opening-remarks"
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.ARCHITECT,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, output=output, result=result),
        )
    assert any("not rubric sections" in error for error in excinfo.value.errors)


async def test_architect_plan_must_fill_the_word_budget(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A plan whose targets do not add up to the brief is refused, not rebalanced."""
    case = load_golden(AgentRole.ARCHITECT, root=repo_root)
    output = deepcopy(case.expected.output)
    for seed in output["sections"]:
        seed["target_words"] = 100
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.ARCHITECT,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, output=output),
        )
    assert any("the gate allows" in error for error in excinfo.value.errors)


async def test_architect_may_add_but_not_drop_drafted_sections(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A re-plan protects work that exists: add sections, never delete them."""
    case = load_golden(AgentRole.ARCHITECT, root=repo_root)
    run_state = type(case.run_state())(
        iter=2,
        brief=case.input.brief,
        sections=(drafted("appendix", title="Appendix"),),
        sources=(),
    )
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.ARCHITECT,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            run_state=run_state,
        )
    assert any("never drop drafted work" in error for error in excinfo.value.errors)


async def test_architect_replan_that_keeps_drafted_sections_is_accepted(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Adding a section to a plan is the reason to re-plan; keeping the old one is legal."""
    case = load_golden(AgentRole.ARCHITECT, root=repo_root)
    run_state = type(case.run_state())(
        iter=2,
        brief=case.input.brief,
        sections=(drafted("introduction", title="Introduction"),),
        sources=(),
    )
    result, _agent, _provider = await run_golden(
        AgentRole.ARCHITECT,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        run_state=run_state,
    )
    assert next(state.id for state in result.sections) == "introduction"
    assert all(state.status is SectionStatus.EMPTY for state in result.sections)


# ------------------------------------------------------------------------- researcher
async def test_researcher_searches_before_the_model_answers(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The search runs first, and its result enters the prompt as untrusted data."""
    case = load_golden(AgentRole.RESEARCHER, root=repo_root)
    tools = researcher_tools(case.expected.result["source_cards"])
    _result, _agent, provider = await run_golden(
        AgentRole.RESEARCHER, repo_root=repo_root, policies=policies, rubric=rubric, tools=tools
    )
    search = tools.get("scholar_search", role=AgentRole.RESEARCHER)
    assert getattr(search, "queries", []), "the researcher made no search"
    assert "token" in search.queries[0].lower()
    system = provider.first_system_text
    assert "<tool_result" in system
    assert "DATA ONLY" in system


async def test_researcher_cannot_invent_a_source(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A card the search never returned and the store never held is refused."""
    case = load_golden(AgentRole.RESEARCHER, root=repo_root)
    invented = deepcopy(case.expected.result["source_cards"][0])
    invented["id"] = new_ulid()
    invented["title"] = "An imaginary survey of token-free writing"
    invented["doi"] = "10.9999/imaginary.2025.001"
    result = deepcopy(case.expected.result)
    result["source_cards"] = [invented]
    output = deepcopy(case.expected.output)
    output["findings"][0]["source_id"] = invented["id"]
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.RESEARCHER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, result=result, output=output),
        )
    assert any("cannot be invented" in error for error in excinfo.value.errors)


async def test_researcher_can_say_nothing_was_found(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The honest empty answer is accepted; an unmarked empty list is not."""
    case = load_golden(AgentRole.RESEARCHER, root=repo_root)
    honest = mutated_answer(
        case,
        result={"source_cards": []},
        output={"findings": [], "no_results": True, "note": "No index returned a match."},
    )
    result, _agent, _provider = await run_golden(
        AgentRole.RESEARCHER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        answer=honest,
        tools=researcher_tools(()),
    )
    assert result.source_cards == []

    unmarked = mutated_answer(case, result={"source_cards": []}, output={"findings": []})
    with pytest.raises(AgentOutputInvalid):
        await run_golden(
            AgentRole.RESEARCHER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=unmarked,
            tools=researcher_tools(()),
        )


def test_researcher_findings_are_capped_at_twelve() -> None:
    """At most twelve cards per task is a schema ceiling, so the loop can budget."""
    findings = [{"source_id": new_ulid(), "finding": "One claim. Another."}] * (MAX_FINDINGS + 1)
    with pytest.raises(ValidationError):
        ResearcherOutput.model_validate({"findings": findings})
    assert MAX_FINDINGS == 12


def test_source_matching_uses_id_doi_or_title_and_year(repo_root: Path) -> None:
    """The anti-fabrication guard agrees with what a citation key can resolve."""
    case = load_golden(AgentRole.RESEARCHER, root=repo_root)
    card = case.expected.result["source_cards"][0]
    known = [SourceCard.model_validate(card)]
    assert matches_known_source(SourceCard.model_validate(card), known)
    # Re-found under a new id with no DOI: same title and year, so the same source.
    refound = SourceCard.model_validate({**card, "id": new_ulid(), "doi": None})
    assert matches_known_source(refound, known)
    other_work = SourceCard.model_validate(
        {
            **card,
            "id": new_ulid(),
            "doi": None,
            "title": "A different paper that merely shares a venue",
            "year": card["year"] + 1,
        }
    )
    assert not matches_known_source(other_work, known), "an unseen work is a new claim"


# ------------------------------------------------------------------------------ writer
async def test_writer_cannot_cite_an_unknown_id(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """An invented marker is caught, and the writer gets exactly one chance to fix it."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    stranger = new_ulid()
    result = deepcopy(case.expected.result)
    result["patches"][0]["ops"][0]["text"] += f" [[CITE:{stranger}]]"
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.WRITER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, result=result),
        )
    assert any("unknown source ids" in error for error in excinfo.value.errors)
    assert excinfo.value.attempts == 2


async def test_writer_must_report_the_ids_it_cited(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The role document is a claim about the patch; a mismatch is an error."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    output = deepcopy(case.expected.output)
    output["cited_source_ids"] = output["cited_source_ids"][:1]
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.WRITER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, output=output),
        )
    assert any("the answer claims" in error for error in excinfo.value.errors)


async def test_writer_refuses_a_task_that_names_two_sections(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A one-section role rejects a two-section task before spending anything."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    task = case.input.task.model_copy(update={"section_ids": ["introduction", "related-work"]})
    provider = RecordingProvider(scripted_provider(AgentRole.WRITER))
    agent = build_agent(
        AgentRole.WRITER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        section_texts=case.section_texts(),
    )
    with pytest.raises(RoleTaskInvalid):
        await agent.run(task, case.run_state())
    assert provider.call_count == 0


async def test_writer_word_window_is_enforced(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A 13-word answer for a 120-word target is a failure, not brevity."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    result = deepcopy(case.expected.result)
    result["patches"][0]["ops"][0]["text"] = (
        "Agentic writing needs budgets [[CITE:01ARZ3NDEKTSV4RRFFQ69G5FC2]]."
    )
    output = deepcopy(case.expected.output)
    output["cited_source_ids"] = ["01ARZ3NDEKTSV4RRFFQ69G5FC2"]
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.WRITER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, result=result, output=output),
        )
    assert any("would carry" in error for error in excinfo.value.errors)
    assert WRITER_WORD_TOLERANCE == 0.10


async def test_writer_never_replaces_a_reviewed_section_wholesale(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A reviewed section is edited with targeted ops, so the reviewer sees the delta."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    section = drafted("introduction", title="Introduction").model_copy(
        update={"status": SectionStatus.REVIEWED}
    )
    run_state = type(case.run_state())(
        iter=1, brief=case.input.brief, sections=(section,), sources=case.input.sources
    )
    answer = mutated_answer(
        case,
        result={"patches": [dict(deepcopy(case.expected.result["patches"][0]), base_version=1)]},
    )
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.WRITER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            run_state=run_state,
            answer=answer,
        )
    assert any("so the reviewer sees the delta" in error for error in excinfo.value.errors)


async def test_writer_reports_missing_input_instead_of_drafting(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """``needs_user_input`` is a valid answer: no patch, named input, ledger status."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    entry = deepcopy(case.expected.result["entry"])
    entry.update({"status": "needs_user_input", "patch_ref": None})
    answer = mutated_answer(
        case,
        result={"entry": entry, "patches": []},
        output={
            "section_id": "introduction",
            "status": "needs_user_input",
            "cited_source_ids": [],
            "missing_inputs": ["The measured results table for section 4."],
        },
    )
    result, _agent, _provider = await run_golden(
        AgentRole.WRITER, repo_root=repo_root, policies=policies, rubric=rubric, answer=answer
    )
    assert result.entry.status is ChangeStatus.NEEDS_USER_INPUT
    assert result.patches == []


async def test_writer_patch_against_a_stale_version_is_refused(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A patch authored against an older version would overwrite someone else's edit."""
    case = load_golden(AgentRole.WRITER, root=repo_root)
    run_state = type(case.run_state())(
        iter=1,
        brief=case.input.brief,
        sections=(drafted("introduction", title="Introduction"),),
        sources=case.input.sources,
    )
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.WRITER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            run_state=run_state,
        )
    assert any("authored against v0" in error for error in excinfo.value.errors)


def test_writer_length_is_measured_on_the_span_that_remains() -> None:
    """A targeted edit is judged on the resulting text, not on how much was typed."""
    patch = Patch(
        section_id="introduction",
        base_version=1,
        patch_id=new_ulid(),
        ops=[{"op": "insert_after", "anchor": "budget.", "text": "one two three"}],
    )
    assert estimated_words(current_text="a budget.", current_words=2, patch=patch) == 5


# ---------------------------------------------------------------------------- reviewer
def test_reviewer_suggested_fix_is_capped() -> None:
    """The 30-word cap is a schema rule, not a style guideline."""
    with pytest.raises(ValidationError):
        ReviewerOutput.model_validate(
            {
                "reviews": [
                    {
                        "section_id": "introduction",
                        "issues": [
                            {
                                "severity": "low",
                                "anchor": "a sentence",
                                "problem": "unclear",
                                "suggested_fix": "word " * (MAX_SUGGESTED_FIX_WORDS + 1),
                            }
                        ],
                    }
                ]
            }
        )


def test_reviewer_cannot_return_a_rewritten_paragraph() -> None:
    """A paragraph does not fit in any reviewer field, by construction."""
    paragraph = "The reviewer is not permitted to return a rewritten paragraph of the article. " * 7
    assert len(paragraph.split()) > 80
    with pytest.raises(ValidationError):
        ReviewerOutput.model_validate(
            {
                "reviews": [
                    {
                        "section_id": "introduction",
                        "issues": [
                            {
                                "severity": "med",
                                "anchor": "sentence",
                                "problem": paragraph,
                                "suggested_fix": "Cut it.",
                            }
                        ],
                    }
                ]
            }
        )


def test_reviewer_returns_at_most_eight_issues_per_section() -> None:
    """Eight is the budget the writer can act on in one pass."""
    issue = {
        "severity": "low",
        "anchor": "a sentence",
        "problem": "unclear",
        "suggested_fix": "Say it plainly.",
    }
    with pytest.raises(ValidationError):
        ReviewerOutput.model_validate(
            {
                "reviews": [
                    {
                        "section_id": "introduction",
                        "issues": [issue] * (MAX_ISSUES_PER_SECTION + 1),
                    }
                ]
            }
        )
    assert MAX_ISSUES_PER_SECTION == 8


def test_reviewer_issues_stay_far_below_a_paragraph(repo_root: Path) -> None:
    """The golden review is the shape we want: anchors locate, fixes hint."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    reviews = ReviewerOutput.model_validate(case.expected.output)
    for review in reviews.reviews:
        assert review.scores, "every review must score the rubric"
        for issue in review.issues:
            assert len(issue.anchor.split()) <= 25
            assert len(issue.problem.split()) <= 80
            assert len(issue.suggested_fix.split()) <= MAX_SUGGESTED_FIX_WORDS


async def test_reviewer_reads_dirty_sections_only(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A final section is settled work; re-reading it wastes tokens."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    settled = case.input.sections[0].model_copy(
        update={"status": SectionStatus.FINAL, "version": 2}
    )
    run_state = type(case.run_state())(
        iter=1, brief=case.input.brief, sections=(settled,), sources=()
    )
    provider = RecordingProvider(scripted_provider(AgentRole.REVIEWER))
    agent = build_agent(
        AgentRole.REVIEWER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        section_texts=case.section_texts(),
    )
    with pytest.raises(RoleTaskInvalid):
        await agent.run(case.input.task, run_state)
    assert provider.call_count == 0


async def test_reviewer_cannot_emit_a_patch(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The schema offers no ``patches`` field; the runtime refuses one anyway."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    entry = deepcopy(case.expected.result["entry"])
    entry["patch_ref"] = "patch:01ARZ3NDEKTSV4RRFFQ69G5FE4"
    patch = {
        "section_id": "introduction",
        "base_version": 1,
        "patch_id": "01ARZ3NDEKTSV4RRFFQ69G5FE4",
        "ops": [{"op": "replace_text", "old": "This article asks", "new": "This paper asks"}],
    }
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.REVIEWER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, result={"entry": entry, "patches": [patch]}),
        )
    assert any("may not return patches" in error for error in excinfo.value.errors)


async def test_reviewer_scores_must_cover_the_rubric(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A partial score sheet would make Q look better while measuring less."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    output = deepcopy(case.expected.output)
    output["reviews"][0]["scores"].pop("coherence")
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.REVIEWER,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, output=output),
        )
    assert any("missing rubric criteria" in error for error in excinfo.value.errors)


async def test_reviewer_records_scores_for_the_validator(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Scores land in the shared scoreboard, tagged with the hash they describe."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    scoreboard = ReviewScoreboard()
    await run_golden(
        AgentRole.REVIEWER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        scoreboard=scoreboard,
    )
    recorded = scoreboard.for_section("introduction")
    assert recorded is not None
    assert recorded.section_hash == case.input.sections[0].hash
    assert set(recorded.scores) == {criterion.name for criterion in rubric.criteria}


# --------------------------------------------------------------------------- validator
async def test_validator_gates_are_the_tool_results(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Every tool-assessed gate is copied from the tool, not from the model."""
    case = load_golden(AgentRole.VALIDATOR, root=repo_root)
    tools = validator_tools(rubric)
    result, _agent, _provider = await run_golden(
        AgentRole.VALIDATOR, repo_root=repo_root, policies=policies, rubric=rubric, tools=tools
    )
    report = result.report
    assert report is not None

    texts = section_texts_of(case)
    sections = [{"id": state.id, "text": texts[state.id]} for state in case.input.sections]
    text_payload = json.loads(
        (
            await tools.invoke(
                "text_checks",
                {
                    "iter": case.input.iter,
                    "article_type": case.input.brief.article_type.value,
                    "target_words": case.input.brief.target_words,
                    "sections": sections,
                },
                role=AgentRole.VALIDATOR,
            )
        ).content
    )
    citation_payload = json.loads(
        (
            await tools.invoke(
                "citation_check",
                {
                    "iter": case.input.iter,
                    "citation_style": case.input.brief.citation_style.value,
                    "sections": sections,
                    "references": texts.get("references", ""),
                    "source_cards": [
                        {"id": card.id, "verification": card.verification.value}
                        for card in case.input.sources
                    ],
                },
                role=AgentRole.VALIDATOR,
            )
        ).content
    )
    for payload in (text_payload, citation_payload):
        for name in payload["assessed_gates"]:
            assert report.gates[name] is payload["report"]["gates"][name], name
    assert report.verified_citation_ratio == pytest.approx(
        citation_payload["verified_citation_ratio"]
    )
    figures_ok, _missing = figure_and_table_gate(texts)
    assert report.gates["referenced_figures_and_tables_exist"] is figures_ok


async def test_validator_gates_ignore_a_model_that_claims_all_is_well(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Subjective answers do not move a hard gate: a TODO still fails the article."""
    case = load_golden(AgentRole.VALIDATOR, root=repo_root)
    from app.agents.roles.inputs import MappingSectionTexts

    polluted = MappingSectionTexts(
        {"introduction": f"{section_texts_of(case)['introduction']} TODO: cite the second card."}
    )
    optimistic = deepcopy(case.expected.output)
    optimistic["subjective_checks"] = dict.fromkeys(optimistic["subjective_checks"], True)
    provider = RecordingProvider(
        scripted_provider(
            AgentRole.VALIDATOR,
            mutated_answer(case, output={"subjective_checks": optimistic["subjective_checks"]}),
        )
    )
    agent = build_agent(
        AgentRole.VALIDATOR,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        tools=validator_tools(rubric),
        section_texts=polluted,
    )
    result = await agent.run(case.input.task, case.run_state())
    assert result.report is not None
    assert result.report.gates["no_placeholders"] is False
    assert result.report.gates["required_sections_present"] is False
    assert result.report.gates["total_words_within_tolerance"] is False


async def test_validator_computes_q_from_the_reviewer_scores(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """Q is the weighted mean of the reviewer's scores, not a model guess."""
    case = load_golden(AgentRole.VALIDATOR, root=repo_root)
    scoreboard = ReviewScoreboard()
    scoreboard.record(
        section_id="introduction",
        section_hash=case.input.sections[0].hash,
        scores={criterion.name: 80 for criterion in rubric.criteria},
        iteration=1,
    )
    result, _agent, _provider = await run_golden(
        AgentRole.VALIDATOR,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        scoreboard=scoreboard,
    )
    assert result.report is not None
    assert result.report.Q == 80
    assert set(result.report.scores) == {criterion.name for criterion in rubric.criteria}


async def test_reviewer_to_validator_round_trip_scores_q(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The two roles share one scoreboard: the reviewer's numbers become the article's Q."""
    reviewer_case = load_golden(AgentRole.REVIEWER, root=repo_root)
    scoreboard = ReviewScoreboard()
    _reviewed, _agent, _provider = await run_golden(
        AgentRole.REVIEWER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        scoreboard=scoreboard,
    )
    scores = {
        name: int(value)
        for name, value in reviewer_case.expected.output["reviews"][0]["scores"].items()
    }
    validated, _vagent, _vprovider = await run_golden(
        AgentRole.VALIDATOR,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        scoreboard=scoreboard,
        run_state=reviewer_case.run_state(),
    )
    assert validated.report is not None
    assert weighted_q(scores, rubric) == validated.report.Q
    assert validated.report.scores == scores


async def test_validator_carries_a_score_forward_only_for_unchanged_text(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A score for text that has changed since is stale; the previous report is used."""
    scoreboard = ReviewScoreboard()
    scoreboard.record(
        section_id="introduction",
        section_hash="sha256:" + "cd" * 32,  # a hash the section no longer has
        scores={criterion.name: 95 for criterion in rubric.criteria},
        iteration=1,
    )
    previous = QualityReport(
        iter=0,
        gates=dict.fromkeys(GATE_NAMES, False),
        scores={criterion.name: 40 for criterion in rubric.criteria},
        Q=40,
        verified_citation_ratio=0.0,
    )
    result, _agent, _provider = await run_golden(
        AgentRole.VALIDATOR,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        scoreboard=scoreboard,
        reports=StaticReportSource(previous),
    )
    assert result.report is not None
    assert result.report.Q == 40


async def test_validator_without_scores_falls_back_to_the_baseline(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """An unscored criterion is average; nothing invents excellence for unmeasured work."""
    result, _agent, _provider = await run_golden(
        AgentRole.VALIDATOR, repo_root=repo_root, policies=policies, rubric=rubric
    )
    assert result.report is not None
    assert result.report.Q == BASELINE_SCORE


async def test_validator_never_accepts_a_model_supplied_report(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A report is computed from tool output; a model's own report is refused."""
    case = load_golden(AgentRole.VALIDATOR, root=repo_root)
    forged = {
        "iter": 1,
        "gates": dict.fromkeys(GATE_NAMES, True),
        "scores": {criterion.name: 100 for criterion in rubric.criteria},
        "Q": 100,
        "verified_citation_ratio": 1.0,
    }
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await run_golden(
            AgentRole.VALIDATOR,
            repo_root=repo_root,
            policies=policies,
            rubric=rubric,
            answer=mutated_answer(case, result={"report": forged}),
        )
    assert any("may not return report" in error for error in excinfo.value.errors)


async def test_validator_needs_its_deterministic_inputs(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """No tools, no report: the validator refuses rather than guessing the gates."""
    case = load_golden(AgentRole.VALIDATOR, root=repo_root)
    provider = RecordingProvider(scripted_provider(AgentRole.VALIDATOR, case.answer_json()))
    agent = build_agent(
        AgentRole.VALIDATOR,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
        section_texts=case.section_texts(),
        tools=None,
    )
    with pytest.raises(ConfigError) as excinfo:
        await agent.run(case.input.task, case.run_state())
    assert "tool registry" in str(excinfo.value)
    assert provider.call_count == 0


async def test_reviewer_needs_the_text_it_reviews(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """A review of text the role cannot read would score an empty article."""
    case = load_golden(AgentRole.REVIEWER, root=repo_root)
    provider = RecordingProvider(scripted_provider(AgentRole.REVIEWER, case.answer_json()))
    agent = build_agent(
        AgentRole.REVIEWER,
        repo_root=repo_root,
        policies=policies,
        rubric=rubric,
        provider=provider,
    )
    with pytest.raises(ConfigError) as excinfo:
        await agent.run(case.input.task, case.run_state())
    assert "section text provider" in str(excinfo.value)
    assert provider.call_count == 0


# ------------------------------------------------------------------------- the wiring
def test_factory_registers_all_five_roles_with_their_collaborators(
    repo_root: Path, policies: Policies, rubric: Rubric
) -> None:
    """The factory is the one wiring path: it registers every role, bound and enabled."""
    from app.agents.binding import MappingBindingResolver
    from app.agents.registry import AgentRegistry
    from app.agents.roles.factory import register_role_agents
    from app.skills.loader import SkillLoader

    bindings = MappingBindingResolver(
        {BindingRole(role.value): agent_binding(role) for role in AgentRole}
    )
    registry = AgentRegistry(bindings=bindings)
    scoreboard = register_role_agents(
        registry,
        provider=scripted_provider(AgentRole.ARCHITECT),
        bindings=bindings,
        governor=governor(policies),
        skills=SkillLoader(repo_root / "skills"),
        rules_dir=repo_root / "rules",
        rubric=rubric,
        tools=validator_tools(rubric),
    )
    assert set(registry.registered_roles()) == set(AgentRole)
    for role, agent_class in ROLE_AGENTS.items():
        agent = registry.resolve(role)
        assert isinstance(agent, agent_class)
        assert agent.tier is agent_class.default_tier
    assert scoreboard is not None


# ---------------------------------------------------------------------- quality maths
def test_figure_gate_reports_a_dangling_reference() -> None:
    """\"As Figure 3 shows\" with no Figure 3 is the failure the gate exists for."""
    ok, missing = figure_and_table_gate({"introduction": "As Figure 3 shows, cost fell."})
    assert ok is False
    assert missing == ["3"]
    ok, missing = figure_and_table_gate(
        {
            "introduction": "As Figure 3 shows, cost fell.",
            "results": "Figure 3. Cost per iteration.",
        }
    )
    assert ok is True
    assert missing == []


def test_weighted_q_uses_the_rubric_weights(rubric: Rubric) -> None:
    """Q is a weighted mean: equal scores stay equal, and missing criteria are ignored."""
    weights = rubric.weights()
    names = list(weights)
    assert weighted_q(dict.fromkeys(names, 100), rubric) == 100
    assert weighted_q(dict.fromkeys(names, 0), rubric) == 0
    assert weighted_q({names[0]: 100}, rubric) == 100
    assert weighted_q({}, rubric) == 0


def test_merge_scores_prefers_fresh_scores_over_carried_ones(rubric: Rubric) -> None:
    """Fresh reviewer scores win; a stale hash falls back to the previous report."""
    state = drafted("introduction")
    scoreboard = ReviewScoreboard()
    scoreboard.record(
        section_id="introduction",
        section_hash=state.hash,
        scores={criterion.name: 90 for criterion in rubric.criteria},
        iteration=1,
    )
    fresh = merge_scores(rubric=rubric, sections=(state,), scoreboard=scoreboard)
    assert set(fresh.values()) == {90}

    stale = drafted("introduction").model_copy(update={"hash": "sha256:" + "ef" * 32})
    carried = merge_scores(
        rubric=rubric,
        sections=(stale,),
        scoreboard=scoreboard,
        previous=QualityReport(
            iter=0,
            gates=dict.fromkeys(GATE_NAMES, False),
            scores={criterion.name: 30 for criterion in rubric.criteria},
            Q=30,
            verified_citation_ratio=0.0,
        ),
    )
    assert set(carried.values()) == {30}
