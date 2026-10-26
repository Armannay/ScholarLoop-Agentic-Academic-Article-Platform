"""Intake: deterministic brief validation and the quick-edit cheap path."""

from __future__ import annotations

import pytest

from app.contracts.enums import AgentRole
from app.orchestrator.intake import (
    IntakeKind,
    IntakeStatus,
    build_quick_edit_task,
    validate_brief,
    validate_payload,
    validate_quick_edit,
)
from tests.orchestrator_support import BRIEF_PAYLOAD, brief

pytestmark = pytest.mark.orchestrator


def test_valid_brief_is_ok() -> None:
    result = validate_brief(dict(BRIEF_PAYLOAD))
    assert result.status is IntakeStatus.OK
    assert result.brief is not None
    assert result.missing_fields == []


@pytest.mark.parametrize(
    "field", ["topic", "article_type", "audience", "language", "target_words", "citation_style"]
)
def test_missing_field_is_named(field: str) -> None:
    payload = dict(BRIEF_PAYLOAD)
    payload.pop(field)
    result = validate_brief(payload)
    assert result.status is IntakeStatus.NEEDS_INPUT
    assert field in result.missing_fields


def test_blank_field_counts_as_missing() -> None:
    payload = dict(BRIEF_PAYLOAD)
    payload["audience"] = "   "
    result = validate_brief(payload)
    assert result.status is IntakeStatus.NEEDS_INPUT
    assert "audience" in result.missing_fields


def test_invalid_enum_is_a_problem_not_a_guess() -> None:
    payload = dict(BRIEF_PAYLOAD)
    payload["article_type"] = "novel"
    result = validate_brief(payload)
    assert result.status is IntakeStatus.NEEDS_INPUT
    assert result.problems  # a stable code names the bad field
    assert result.brief is None


def test_vague_topic_and_bad_language_need_input() -> None:
    payload = dict(BRIEF_PAYLOAD)
    payload["topic"] = "Writing stuff"
    payload["language"] = "english-please!"
    result = validate_brief(payload)
    assert result.status is IntakeStatus.NEEDS_INPUT
    assert "topic_too_vague" in result.problems
    assert "language_not_bcp47" in result.problems


def test_brief_instance_passes_through() -> None:
    assert validate_brief(brief()).status is IntakeStatus.OK


def test_quick_edit_routing_and_task() -> None:
    result = validate_payload(
        {"section_id": "introduction", "instructions": "Tighten the opening paragraph."}
    )
    assert result.status is IntakeStatus.OK
    assert result.kind is IntakeKind.QUICK_EDIT
    assert result.quick_edit is not None
    task = build_quick_edit_task(result.quick_edit)
    assert task.role is AgentRole.WRITER
    assert task.section_ids == ["introduction"]
    assert task.depends_on == []


def test_quick_edit_missing_fields() -> None:
    result = validate_quick_edit({"section_id": "introduction"})
    assert result.status is IntakeStatus.NEEDS_INPUT
    assert result.missing_fields == ["instructions"]


def test_explicit_kind_and_unknown_kind() -> None:
    brief_result = validate_payload({"kind": "brief", **BRIEF_PAYLOAD})
    assert brief_result.kind is IntakeKind.BRIEF and brief_result.ok
    unknown = validate_payload({"kind": "poem", **BRIEF_PAYLOAD})
    assert unknown.status is IntakeStatus.NEEDS_INPUT
    assert unknown.problems == ["unknown_request_kind"]
