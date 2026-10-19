"""Tests for planner prompt assembly (:mod:`app.planner.prompts`).

Pins HARD RULE 3 (references, not content: no section bodies, no source
snippets), HARD RULE 4 (interpolated text fenced as data), the cacheable
stable-prefix layout, and deterministic rendering (identical state -> identical
request), so provider caching and MockProvider scripting stay predictable.
"""

from __future__ import annotations

from app.contracts.brief import Brief
from app.contracts.enums import MessageRole
from app.contracts.plan import REQUIRED_ROLES
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.core.rubric import Rubric
from app.planner.context import PlannerContext
from app.planner.prompts import (
    build_plan_request,
    build_repair_request,
    build_verdict_request,
    render_sources,
)
from tests.factories import SAMPLE_BRIEF, sample_payloads, sample_ulid

SNIPPET_SENTINEL = "SENTINEL-SNIPPET-TEXT-should-never-reach-the-planner"
SECTION_HASH = "sha256:" + "ab" * 32


def make_context(rubric: Rubric, **overrides: object) -> PlannerContext:
    """A planner context with one section, one card, a report, digest and feedback."""
    section = SectionState.model_validate(sample_payloads()["SectionState"])
    card_payload = dict(sample_payloads()["SourceCard"])
    card_payload["snippet"] = SNIPPET_SENTINEL
    card = SourceCard.model_validate(card_payload)
    report = QualityReport.model_validate(sample_payloads()["QualityReport"])
    fields: dict[str, object] = {
        "brief": Brief.model_validate(SAMPLE_BRIEF),
        "rubric": rubric,
        "digest": "DIGEST-SENTINEL: iteration 3 drafted the introduction.",
        "sections": (section,),
        "sources": (card,),
        "report": report,
        "user_feedback": "FEEDBACK-SENTINEL: keep the tone formal.",
    }
    fields.update(overrides)
    return PlannerContext(**fields)  # type: ignore[arg-type]


def plan_request(rubric: Rubric, **overrides: object):  # type: ignore[no-untyped-def]
    """A plan request built from the shared context factory."""
    return build_plan_request(
        make_context(rubric, **overrides),
        mode="initial",
        expected_version=1,
        require_roles=REQUIRED_ROLES,
        budget_cap=800_000,
        max_output_tokens=4_096,
        max_source_refs=100,
    )


def user_text(request) -> str:  # type: ignore[no-untyped-def]
    """The single user message's text."""
    assert len(request.messages) == 1
    assert request.messages[0].role is MessageRole.USER
    return request.messages[0].text


def test_plan_request_cache_layout_schema_and_caps(rubric: Rubric) -> None:
    request = plan_request(rubric)

    assert [block.cache for block in request.system_blocks] == [True, True]
    assert "never write article text" in request.system_blocks[0].text
    assert "methodology" in request.system_blocks[1].text  # rubric for research
    assert request.json_schema is not None
    assert request.json_schema["title"] == "Plan"
    assert request.max_output_tokens == 4_096

    text = user_text(request)
    assert "Token-frugal orchestration" in text  # brief topic
    assert "T1, T2" in text  # id-label convention (server re-mints ULIDs)
    assert "DIGEST-SENTINEL" in text and "FEEDBACK-SENTINEL" in text
    assert text.count("<data>") >= 3  # brief, digest, feedback are fenced as data


def test_plan_request_carries_references_not_content(rubric: Rubric) -> None:
    request = plan_request(rubric)
    text = user_text(request)

    assert SNIPPET_SENTINEL not in text  # card snippet never reaches the Planner
    assert SECTION_HASH not in text  # hashes are bookkeeping, not planning input
    assert "introduction status=draft v2 words=412" in text
    assert "citations=1/2 resolved" in text
    assert sample_payloads()["SourceCard"]["id"] in text  # cards by id (HARD RULE 3)


def test_plan_request_is_deterministic(rubric: Rubric) -> None:
    assert plan_request(rubric).model_dump(mode="json") == plan_request(rubric).model_dump(
        mode="json"
    )


def test_plan_request_without_history_omits_digest_and_feedback(rubric: Rubric) -> None:
    request = plan_request(rubric, digest=None, user_feedback=None, report=None)
    text = user_text(request)
    assert "digest claude.md" not in text
    assert "user feedback" not in text
    assert "Latest quality report" not in text


def test_verdict_request_schema_and_failed_gates(rubric: Rubric) -> None:
    request = build_verdict_request(
        make_context(rubric), q_min=70, max_output_tokens=1_024, max_source_refs=100
    )
    assert request.json_schema is not None
    assert request.json_schema["title"] == "Verdict"
    assert request.max_output_tokens == 1_024
    text = user_text(request)
    assert "Q=67" in text
    assert "Q >= 70" in text


def test_repair_request_is_context_free_and_bounded() -> None:
    previous = '{"tasks": []}' + "x" * 20_000
    request = build_repair_request(
        contract_name="Plan",
        previous_text=previous,
        issues=["missing required role(s): validator", "tasks: too short"],
        max_output_tokens=2_048,
    )
    text = user_text(request)
    assert request.json_schema is not None
    assert request.json_schema["title"] == "Plan"
    assert request.system_blocks[0].cache is True
    assert "missing required role(s): validator" in text
    assert "[truncated for repair]" in text
    assert len(text) < len(previous)


def test_render_sources_limit_notes_omissions_without_guessing(rubric: Rubric) -> None:
    cards = []
    for seed in range(5):
        payload = dict(sample_payloads()["SourceCard"])
        payload["id"] = sample_ulid(50 + seed)
        payload["year"] = 2020 + seed
        cards.append(SourceCard.model_validate(payload))

    rendered = render_sources(cards, limit=2)

    assert "+3 more cards not listed" in rendered
    assert render_sources([], limit=2).startswith("(none")


def test_context_known_ids_include_rubric_state_and_abstract(rubric: Rubric) -> None:
    context = make_context(rubric)
    known = context.known_section_ids()
    assert {"introduction", "methodology", "abstract"} <= known
    assert context.known_source_ids() == {sample_payloads()["SourceCard"]["id"]}
