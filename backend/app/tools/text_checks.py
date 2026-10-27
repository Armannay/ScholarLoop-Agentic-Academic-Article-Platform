"""Rubric-backed deterministic structural and prose checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from app.contracts.common import SECTION_ID_PATTERN, word_count
from app.contracts.enums import ArticleType, IssueSeverity
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.core.rubric import Rubric, load_rubric, resolve_sections
from app.tools.base import ToolContext, ToolResult

__all__ = ["TextChecksTool"]

MAX_TEXT_CHECK_INPUT_CHARS = 2_000_000
_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+[^\n]*|[ \t]*)$")
_EMPTY_HEADING_LINE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*#*[ \t]*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _empty_headings(text: str) -> list[str]:
    headings = list(_HEADING.finditer(text))
    empty: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = _COMMENT.sub("", text[heading.end() : end]).strip()
        if not body or _EMPTY_HEADING_LINE.fullmatch(heading.group()):
            empty.append(heading.group().strip())
    return empty


def _has_body(text: str) -> bool:
    return bool(_HEADING.sub("", _COMMENT.sub("", text)).strip())


class TextChecksTool:
    """Evaluate required sections, abstract range, target length and placeholders."""

    _NAME: ClassVar[str] = "text_checks"
    _DESCRIPTION: ClassVar[str] = (
        "Check required sections, rubric abstract length, section/total words, and placeholders."
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
        self._placeholder_patterns = (
            *(re.compile(pattern) for pattern in self._rubric.text_checks.placeholder_patterns),
            # The rubric's default catches "lorem ipsum"; the task also treats a
            # standalone "lorem" as a placeholder without double-counting the pair.
            re.compile(r"(?i)\blorem\b(?!\s+ipsum\b)"),
        )

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        article_type = ArticleType(str(arguments["article_type"]))
        sections_raw = arguments["sections"]
        if not all(isinstance(item, Mapping) for item in sections_raw):
            return ToolResult(tool=self.name, ok=False, content="sections must contain objects")
        ids = [str(item["id"]) for item in sections_raw]
        if any(not re.fullmatch(SECTION_ID_PATTERN, section_id) for section_id in ids):
            return ToolResult(tool=self.name, ok=False, content="invalid section id")
        if len(ids) != len(set(ids)):
            return ToolResult(tool=self.name, ok=False, content="duplicate section ids")
        section_texts = {str(item["id"]): str(item["text"]) for item in sections_raw}
        if sum(map(len, section_texts.values())) > MAX_TEXT_CHECK_INPUT_CHARS:
            return ToolResult(
                tool=self.name, ok=False, content="combined section text exceeds limit"
            )

        spec = self._rubric.spec_for(article_type)
        resolved = resolve_sections(self._rubric, article_type)
        required_ids = [section.id for section in resolved if section.required]
        missing_sections = [
            section_id
            for section_id in required_ids
            if not _has_body(section_texts.get(section_id, ""))
        ]
        issues: list[QualityIssue] = []
        issue_count = 0

        def add_issue(message: str, *, section_id: str | None = None) -> None:
            nonlocal issue_count
            issue_count += 1
            if len(issues) < 19:
                issues.append(
                    QualityIssue(
                        id=f"ISSUE-text-checks-{issue_count:03d}",
                        section_id=section_id,
                        severity=IssueSeverity.HIGH,
                        text=message[:600],
                    )
                )

        for section_id in missing_sections:
            add_issue(
                f"Required section {section_id!r} is missing or empty.", section_id=section_id
            )

        section_words = {section_id: word_count(text) for section_id, text in section_texts.items()}
        abstract_text = section_texts.get("abstract", "")
        abstract_words = section_words.get("abstract", 0)
        abstract_valid = (not spec.abstract.required and not abstract_text.strip()) or (
            _has_body(abstract_text)
            and spec.abstract.min_words <= abstract_words <= spec.abstract.max_words
        )
        if not abstract_valid:
            add_issue(
                f"Abstract has {abstract_words} words; rubric range is "
                f"{spec.abstract.min_words}..{spec.abstract.max_words}.",
                section_id="abstract",
            )

        no_prose_ids = {section.id for section in spec.sections if not section.accepts_prose}
        total_words = sum(
            count for section_id, count in section_words.items() if section_id not in no_prose_ids
        )
        target_words = int(arguments["target_words"])
        low_words = int(target_words * 0.85)
        high_words = int(target_words * 1.15)
        total_valid = low_words <= total_words <= high_words
        if not total_valid:
            add_issue(
                f"Article has {total_words} words; target tolerance is "
                f"{low_words}..{high_words} words."
            )

        placeholder_count = 0
        empty_heading_count = 0
        for section_id, text in section_texts.items():
            for pattern in self._placeholder_patterns:
                for match in pattern.finditer(text):
                    placeholder_count += 1
                    add_issue(
                        f"Placeholder text {match.group(0)[:100]!r} remains and must be resolved.",
                        section_id=section_id,
                    )
            for heading in _empty_headings(text):
                empty_heading_count += 1
                add_issue(f"Heading {heading[:100]!r} has no content.", section_id=section_id)

        gates = dict.fromkeys(GATE_NAMES, False)
        gates["required_sections_present"] = not missing_sections
        gates["abstract_within_range"] = abstract_valid
        gates["total_words_within_tolerance"] = total_valid
        gates["no_placeholders"] = placeholder_count == 0 and empty_heading_count == 0
        omitted = issue_count - len(issues)
        if omitted:
            issues.append(
                QualityIssue(
                    id="ISSUE-text-checks-summary",
                    severity=IssueSeverity.MED,
                    text=f"{omitted} additional text issue(s) were summarized.",
                )
            )
        report = QualityReport(
            iter=int(arguments["iter"]),
            gates=gates,
            Q=0,
            verified_citation_ratio=0.0,
            issues=issues,
        )
        payload: dict[str, Any] = {
            "report": report.model_dump(mode="json"),
            "assessed_gates": [
                "required_sections_present",
                "abstract_within_range",
                "total_words_within_tolerance",
                "no_placeholders",
            ],
            "metrics": {
                "section_words": section_words,
                "total_words": total_words,
                "target_words": target_words,
                "abstract_words": abstract_words,
                "required_sections": required_ids,
                "missing_sections": missing_sections,
                "placeholder_count": placeholder_count,
                "empty_heading_count": empty_heading_count,
                "omitted_issue_count": omitted,
                "omitted_section_word_counts": 0,
            },
        }
        truncated = bool(omitted)
        while True:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content) <= 7_500:
                break
            word_counts = payload["metrics"]["section_words"]
            if word_counts:
                word_counts.pop(next(reversed(word_counts)))
                payload["metrics"]["omitted_section_word_counts"] += 1
            elif payload["report"]["issues"]:
                removed = payload["report"]["issues"].pop()
                if removed["id"] != "ISSUE-text-checks-summary":
                    payload["metrics"]["omitted_issue_count"] += 1
            else:
                break
            truncated = True
        return ToolResult(tool=self.name, content=content, truncated=truncated)
