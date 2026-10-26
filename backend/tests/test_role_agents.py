"""Role schema regression tests use local golden fixtures; no provider calls."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.roles.schemas import (
    ArchitectOutput,
    ResearcherOutput,
    ReviewerOutput,
    ValidatorOutput,
    WriterOutput,
)

PROMPTS = Path(__file__).parents[2] / "prompts"


@pytest.mark.parametrize(
    "name,model",
    [
        ("architect", ArchitectOutput),
        ("researcher", ResearcherOutput),
        ("writer", WriterOutput),
        ("reviewer", ReviewerOutput),
        ("validator", ValidatorOutput),
    ],
)
def test_golden_expected_matches_role_schema(name, model):
    fixture = json.loads((PROMPTS / f"{name}.golden.json").read_text(encoding="utf-8"))
    model.model_validate(fixture["expected"])


def test_reviewer_suggested_fix_is_capped():
    with pytest.raises(ValidationError):
        ReviewerOutput.model_validate(
            {
                "reviews": [
                    {
                        "section_id": "intro",
                        "issues": [
                            {
                                "severity": "low",
                                "anchor": "sentence",
                                "problem": "unclear",
                                "suggested_fix": "word " * 40,
                            }
                        ],
                    }
                ]
            }
        )


def test_writer_schema_rejects_extra_fields():
    with pytest.raises(ValidationError):
        WriterOutput.model_validate(
            {
                "section_id": "intro",
                "patch": {},
                "status": "ok",
                "surprise": True,
            }
        )
