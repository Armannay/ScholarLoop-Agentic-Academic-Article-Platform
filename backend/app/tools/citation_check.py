"""Deterministic citation/reference consistency checks and partial QualityReports."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, ClassVar

from app.contracts.enums import CitationStyle, IssueSeverity, VerificationStatus
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.core.ids import is_ulid
from app.tools.base import ToolContext, ToolResult

__all__ = ["CitationCheckTool", "verified_citation_ratio"]

MAX_CITATION_INPUT_CHARS = 2_000_000
_IEEE_MARKER = re.compile(r"\[(\d+(?:\s*[,;–-]\s*\d+)*)\]")
_APA_MARKER = re.compile(r"\(([^()]{1,180}?(?:18|19|20|21)\d{2}[a-z]?[^()]*)\)", re.IGNORECASE)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\])}>,;]+", re.IGNORECASE)


def _normal_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def _numeric_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _IEEE_MARKER.finditer(text):
        for number in re.split(r"\s*[,;–-]\s*", match.group(1)):
            keys.append(number.strip())
    return keys


def _apa_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _APA_MARKER.finditer(text):
        for citation in match.group(1).split(";"):
            key = " ".join(citation.strip().split())
            if re.search(r"(?:18|19|20|21)\d{2}", key):
                keys.append(key)
    return keys


def _reference_key(value: str) -> str:
    key = value.strip()
    if key.startswith("[") and key.endswith("]"):
        key = key[1:-1].strip()
    return key


def _issue(index: int, message: str, severity: IssueSeverity) -> QualityIssue:
    return QualityIssue(
        id=f"ISSUE-citation-check-{index:03d}", severity=severity, text=message[:600]
    )


def verified_citation_ratio(
    cited_reference_keys: list[str],
    source_ids_by_key: Mapping[str, str | None],
    verification_by_id: Mapping[str, VerificationStatus],
) -> float:
    """Match SourceStore semantics: unique cited ids, missing ids count unverified."""
    cited_ids: set[str] = set()
    missing_keys: set[str] = set()
    for key in cited_reference_keys:
        normalized = _normal_key(key)
        source_id = source_ids_by_key.get(normalized)
        if source_id:
            cited_ids.add(source_id)
        else:
            missing_keys.add(normalized)
    denominator = len(cited_ids) + len(missing_keys)
    if denominator == 0:
        return 0.0
    verified = sum(
        verification_by_id.get(source_id) is VerificationStatus.VERIFIED for source_id in cited_ids
    )
    return verified / denominator


class CitationCheckTool:
    """Check in-text markers against explicit reference keys and source-card statuses."""

    _NAME: ClassVar[str] = "citation_check"
    _DESCRIPTION: ClassVar[str] = (
        "Check orphan, uncited, duplicate, unresolved, and unverified references in APA 7 or IEEE."
    )
    _TOKEN_COST: ClassVar[int] = 18
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "iter": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "citation_style": {
                "type": "string",
                "enum": [style.value for style in CitationStyle],
            },
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
            "references": {
                "type": "array",
                "maxItems": 500,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "minLength": 1, "maxLength": 200},
                        "text": {"type": "string", "maxLength": 4_000},
                        "source_id": {"type": ["string", "null"], "maxLength": 26},
                    },
                    "required": ["key", "text"],
                    "additionalProperties": False,
                },
            },
            "source_cards": {
                "type": "array",
                "maxItems": 500,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 26, "maxLength": 26},
                        "verification": {
                            "type": "string",
                            "enum": [status.value for status in VerificationStatus],
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": True,
                },
            },
        },
        "required": ["iter", "citation_style", "sections", "references", "source_cards"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.name = self._NAME
        self.description = self._DESCRIPTION
        self.token_cost = self._TOKEN_COST
        self.parameters = dict(self._PARAMETERS)

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        style = CitationStyle(str(arguments["citation_style"]))
        sections = arguments["sections"]
        references = arguments["references"]
        cards = arguments["source_cards"]
        if not all(isinstance(item, Mapping) for item in (*sections, *references, *cards)):
            return ToolResult(
                tool=self.name,
                ok=False,
                content="sections, references and source_cards must be objects",
            )

        body = "\n".join(
            str(section["text"])
            for section in sections
            if str(section.get("id", "")).casefold() != "references"
        )
        reference_chars = sum(
            len(str(reference.get("key", ""))) + len(str(reference.get("text", "")))
            for reference in references
        )
        if len(body) + reference_chars > MAX_CITATION_INPUT_CHARS:
            return ToolResult(
                tool=self.name,
                ok=False,
                content="combined citation and reference text exceeds the tool input limit",
            )
        numeric_markers = _numeric_keys(body)
        author_date_markers = _apa_keys(body)
        if style in {CitationStyle.IEEE, CitationStyle.VANCOUVER}:
            cited_keys = numeric_markers
        else:
            # Harvard author-date markers are not sufficiently specified by contracts-v1.
            cited_keys = author_date_markers

        normalized_ref_keys: list[str] = []
        source_ids_by_key: dict[str, str | None] = {}
        duplicate_reference_indices: set[int] = set()
        seen_reference_ids: set[str] = set()
        seen_dois: set[str] = set()
        seen_texts: set[str] = set()
        for index, reference in enumerate(references):
            key = _reference_key(str(reference["key"]))
            normalized = _normal_key(key)
            normalized_ref_keys.append(normalized)
            source_id_raw = reference.get("source_id")
            source_id = str(source_id_raw) if source_id_raw is not None else None
            source_ids_by_key[normalized] = source_id
            text_norm = _normal_key(str(reference["text"]))
            doi_match = _DOI_RE.search(str(reference["text"]))
            doi_key = doi_match.group(0).rstrip(".").casefold() if doi_match else ""
            is_duplicate = (
                normalized in seen_reference_ids
                or (source_id is not None and source_id in seen_reference_ids)
                or (doi_key != "" and doi_key in seen_dois)
                or (text_norm != "" and text_norm in seen_texts)
            )
            if is_duplicate:
                duplicate_reference_indices.add(index)
            seen_reference_ids.add(normalized)
            if source_id is not None:
                seen_reference_ids.add(source_id)
            if doi_key:
                seen_dois.add(doi_key)
            if text_norm:
                seen_texts.add(text_norm)

        cited_normalized = {_normal_key(key) for key in cited_keys}
        available_refs = set(normalized_ref_keys)
        orphan_keys = sorted(cited_normalized - available_refs)
        uncited_keys = sorted(available_refs - cited_normalized)
        card_status: dict[str, VerificationStatus] = {}
        for card in cards:
            source_id = str(card["id"])
            if not is_ulid(source_id):
                continue
            try:
                card_status[source_id] = VerificationStatus(
                    str(card.get("verification", VerificationStatus.UNKNOWN.value))
                )
            except ValueError:
                continue

        issues: list[QualityIssue] = []
        for key in orphan_keys:
            issues.append(
                _issue(
                    len(issues) + 1,
                    f"In-text citation {key!r} has no reference entry.",
                    IssueSeverity.HIGH,
                )
            )
        for key in uncited_keys:
            issues.append(
                _issue(
                    len(issues) + 1,
                    f"Reference {key!r} is not cited in the article text.",
                    IssueSeverity.MED,
                )
            )
        for index in sorted(duplicate_reference_indices):
            issues.append(
                _issue(
                    len(issues) + 1,
                    f"Reference entry {references[index]['key']!r} duplicates another reference.",
                    IssueSeverity.MED,
                )
            )
        for normalized in sorted(available_refs):
            source_id = source_ids_by_key.get(normalized)
            if source_id is None or not is_ulid(source_id) or source_id not in card_status:
                issues.append(
                    _issue(
                        len(issues) + 1,
                        f"Reference {normalized!r} has an unresolved source id.",
                        IssueSeverity.HIGH,
                    )
                )
            elif card_status[source_id] is not VerificationStatus.VERIFIED:
                issues.append(
                    _issue(
                        len(issues) + 1,
                        f"Reference source {source_id} is {card_status[source_id].value}; "
                        "validator verification is required.",
                        IssueSeverity.MED,
                    )
                )
        if style in {CitationStyle.HARVARD, CitationStyle.VANCOUVER}:
            issues.append(
                _issue(
                    len(issues) + 1,
                    f"Automated {style.value} style checking is not implemented; review manually.",
                    IssueSeverity.MED,
                )
            )

        if style is CitationStyle.IEEE:
            citations_formatted = not author_date_markers and all(
                bool(re.fullmatch(r"\d+", key)) for key in cited_keys
            )
        elif style is CitationStyle.APA7:
            citations_formatted = not numeric_markers and all(
                bool(re.search(r"(?:18|19|20|21)\d{2}", key)) for key in cited_keys
            )
        else:
            citations_formatted = False
        omitted_issue_count = max(0, len(issues) - 20)
        gates = dict.fromkeys(GATE_NAMES, False)
        gates["citations_resolve_both_ways"] = (
            not orphan_keys and not uncited_keys and not duplicate_reference_indices
        )
        # A full issue list is required for this gate; overflow fails closed.
        gates["references_verified_or_flagged"] = omitted_issue_count == 0
        gates["citation_style_matches"] = citations_formatted and style in {
            CitationStyle.APA7,
            CitationStyle.IEEE,
        }
        verification_ratio = verified_citation_ratio(
            cited_keys,
            source_ids_by_key,
            card_status,
        )
        if omitted_issue_count:
            report_issues = issues[:19]
            report_issues.append(
                _issue(
                    20,
                    f"{omitted_issue_count} additional citation issue(s) were summarized.",
                    IssueSeverity.MED,
                )
            )
        else:
            report_issues = issues
        report = QualityReport(
            iter=int(arguments["iter"]),
            gates=gates,
            Q=0,
            verified_citation_ratio=verification_ratio,
            issues=report_issues,
        )
        payload = {
            "report": report.model_dump(mode="json"),
            "assessed_gates": [
                "citations_resolve_both_ways",
                "references_verified_or_flagged",
                "citation_style_matches",
            ],
            "citation_count": len(cited_keys),
            "reference_count": len(references),
        }
        return ToolResult(
            tool=self.name,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
