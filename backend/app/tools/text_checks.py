"""Rubric-backed deterministic structural and prose checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from app.contracts.common import word_count
from app.contracts.enums import ArticleType, IssueSeverity
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.core.rubric import Rubric, load_rubric, resolve_sections
from app.tools.base import ToolContext, ToolResult

__all__ = ["TextChecksTool"]

MAX_TEXT_CHECK_INPUT_CHARS = 2_000_000


class TextChecksTool:
    """Evaluate required sections, abstract range, target length and placeholders."""

    _NAME: ClassVar[str] = "text_checks"
    _DESCRIPTION: ClassVar[str] = (
        "Check required sections, rubric abstract length, total words, and placeholders."
    )
    _TOKEN_COST: ClassVar[int] = 18
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "iter": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "article_type": {"type": "string", "enum": [value.value for value in ArticleType]},
            "target_words": {"type": "integer", "minimum": 300, "maximum": 30_000},
            "sections": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "text": {"type": "string", "maxLength": 500_000},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["iter", "article_type", "target_words", "sections"],
        "additionalProperties": False,
    }

    def __init__(self, *, rubric_path: Path | None = None, rubric: Rubric | None = None) -> None:
        self.name = self._NAME
        self.description = self._DESCRIPTION
        self.token_cost = self._TOKEN_COST
        self.parameters = dict(self._PARAMETERS)
        root_rubric = Path(__file__).resolve().parents[3] / "config" / "rubric.yaml"
        self._rubric = rubric or load_rubric(rubric_path or root_rubric)
        self._placeholder_patterns = tuple(
            re.compile(pattern) for pattern in self._rubric.text_checks.placeholder_patterns
        )

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        article_type = ArticleType(str(arguments["article_type"]))
        sections_raw = arguments["sections"]
        if not all(isinstance(item, Mapping) for item in sections_raw):
            return ToolResult(tool=self.name, ok=False, content="sections must contain objects")
        section_texts = {str(item["id"]): str(item["text"]) for item in sections_raw}
        if sum(map(len, section_texts.values())) > MAX_TEXT_CHECK_INPUT_CHARS:
            return ToolResult(
                tool=self.name,
                ok=False,
                content="combined section text exceeds the deterministic check input limit",
            )
        spec = self._rubric.spec_for(article_type)
        resolved = resolve_sections(self._rubric, article_type)
        required_ids = [section.id for section in resolved if section.required]
        missing_sections = [
            section_id
            for section_id in required_ids
            if not section_texts.get(section_id, "").strip()
        ]
        issues: list[QualityIssue] = []
        for section_id in missing_sections:
            issues.append(
                QualityIssue(
                    id=f"ISSUE-text-checks-{len(issues) + 1:03d}",
                    section_id=section_id,
                    severity=IssueSeverity.HIGH,
                    text=f"Required section {section_id!r} is missing or empty.",
                )
            )

        abstract_text = section_texts.get("abstract", "")
        abstract_words = word_count(abstract_text)
        abstract_valid = (not spec.abstract.required and not abstract_text) or (
            spec.abstract.min_words <= abstract_words <= spec.abstract.max_words
        )
        if spec.abstract.required and not abstract_text.strip():
            abstract_valid = False
        if not abstract_valid:
            issues.append(
                QualityIssue(
                    id=f"ISSUE-text-checks-{len(issues) + 1:03d}",
                    section_id="abstract",
                    severity=IssueSeverity.HIGH,
                    text=(
                        f"Abstract has {abstract_words} words; rubric range is "
                        f"{spec.abstract.min_words}..{spec.abstract.max_words}."
                    ),
                )
            )

        no_prose_ids = {section.id for section in spec.sections if not section.accepts_prose}
        total_words = sum(
            word_count(text)
            for section_id, text in section_texts.items()
            if section_id not in no_prose_ids
        )
        target_words = int(arguments["target_words"])
        low_words = int(target_words * 0.85)
        high_words = int(target_words * 1.15)
        total_valid = low_words <= total_words <= high_words
        if not total_valid:
            issues.append(
                QualityIssue(
                    id=f"ISSUE-text-checks-{len(issues) + 1:03d}",
                    severity=IssueSeverity.HIGH,
                    text=(
                        f"Article has {total_words} words; target tolerance is "
                        f"{low_words}..{high_words} words."
                    ),
                )
            )

        placeholder_matches: list[tuple[str, str]] = []
        for section_id, text in section_texts.items():
            for pattern in self._placeholder_patterns:
                match = pattern.search(text)
                if match is not None:
                    placeholder_matches.append((section_id, match.group(0)))
        for section_id, marker in placeholder_matches:
            if len(issues) >= 200:
                break
            issues.append(
                QualityIssue(
                    id=f"ISSUE-text-checks-{len(issues) + 1:03d}",
                    section_id=section_id,
                    severity=IssueSeverity.HIGH,
                    text=f"Placeholder text {marker!r} remains and must be resolved.",
                )
            )

        gates = dict.fromkeys(GATE_NAMES, False)
        gates["required_sections_present"] = not missing_sections
        gates["abstract_within_range"] = abstract_valid
        gates["total_words_within_tolerance"] = total_valid
        gates["no_placeholders"] = not placeholder_matches
        report_issues = issues[:19]
        if len(issues) > 19:
            report_issues.append(
                QualityIssue(
                    id="ISSUE-text-checks-200",
                    severity=IssueSeverity.MED,
                    text=f"{len(issues) - 19} additional text issue(s) were summarized.",
                )
            )
        report = QualityReport(
            iter=int(arguments["iter"]),
            gates=gates,
            Q=0,
            verified_citation_ratio=0.0,
            issues=report_issues,
        )
        payload = {
            "report": report.model_dump(mode="json"),
            "assessed_gates": [
                "required_sections_present",
                "abstract_within_range",
                "total_words_within_tolerance",
                "no_placeholders",
            ],
            "metrics": {
                "total_words": total_words,
                "target_words": target_words,
                "abstract_words": abstract_words,
                "missing_sections": missing_sections,
                "placeholder_count": len(placeholder_matches),
            },
        }
        return ToolResult(
            tool=self.name,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
