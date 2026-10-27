"""Deterministic citation/reference checks and partial QualityReports.

Reference entries can be structured objects or a one-entry-per-line list. A raw
entry may carry ``[[CITE:source_id]]`` to associate it with a SourceCard. Style
checks are deliberately shallow regex checks, not a complete citation parser.
"""

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
_MAX_CITATION_MARKERS = 10_000
_MAX_RESULT_CHARS = 7_500
_MAX_DIAGNOSTICS = 20
_CITE_MARKER = re.compile(r"\[\[CITE:([^\]\r\n]{0,1000})\]\]", re.IGNORECASE)
_CITE_PREFIX = re.compile(r"\[\[CITE:", re.IGNORECASE)
_IEEE_MARKER = re.compile(r"\[(\d+(?:\s*[,;–-]\s*\d+)*)\]")
_APA_MARKER = re.compile(r"\(([^()]{1,180}?(?:18|19|20|21)\d{2}[a-z]?[^()]*)\)", re.IGNORECASE)
_APA_NARRATIVE = re.compile(
    r"\b([A-Z][\w'’.-]+(?:\s+et\s+al\.)?)\s*\(((?:18|19|20|21)\d{2}[a-z]?)\)"
)
_APA_CITATION = re.compile(r"^[^\d()]{1,140},\s*(?:18|19|20|21)\d{2}[a-z]?(?:,.*)?$", re.I)
_APA_REFERENCE = re.compile(
    r"^\s*(?:\d+\.\s*)?[^\d\n]{1,200}\s+\((?:18|19|20|21)\d{2}[a-z]?\)\.\s+\S",
    re.I,
)
_YEAR = re.compile(r"\b(?:18|19|20|21)\d{2}\b")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\])}>,;]+", re.IGNORECASE)


def _normal_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def _numeric_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _IEEE_MARKER.finditer(text):
        if len(match.group(1)) > 2 * _MAX_CITATION_MARKERS:
            raise ValueError("too many numeric citation markers")
        for part in re.split(r"\s*[,;]\s*", match.group(1)):
            range_match = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
            if range_match:
                start, end = map(int, range_match.groups())
                if start <= end and end - start <= 50:
                    keys.extend(str(number) for number in range(start, end + 1))
                else:
                    keys.append(part)
            else:
                keys.append(part.strip())
            if len(keys) > _MAX_CITATION_MARKERS:
                raise ValueError("too many numeric citation markers")
    return keys


def _apa_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _APA_MARKER.finditer(text):
        for citation in match.group(1).split(";"):
            key = " ".join(citation.strip().split())
            if re.search(r"(?:18|19|20|21)\d{2}", key):
                keys.append(key)
                if len(keys) > _MAX_CITATION_MARKERS:
                    raise ValueError("too many author-date citation markers")
    for match in _APA_NARRATIVE.finditer(text):
        keys.append(f"{match.group(1)}, {match.group(2)}")
        if len(keys) > _MAX_CITATION_MARKERS:
            raise ValueError("too many author-date citation markers")
    return keys


def _reference_key(value: str) -> str:
    key = value.strip()
    if key.startswith("[") and key.endswith("]"):
        key = key[1:-1].strip()
    return key


def _label(value: str) -> str:
    return repr(value if len(value) <= 100 else value[:97] + "...")


def _issue(index: int, message: str, severity: IssueSeverity) -> QualityIssue:
    return QualityIssue(
        id=f"ISSUE-citation-check-{index:03d}", severity=severity, text=message[:600]
    )


def verified_citation_ratio(
    cited_reference_keys: list[str],
    source_ids_by_key: Mapping[str, str | None],
    verification_by_id: Mapping[str, VerificationStatus],
) -> float:
    """Fraction of unique cited sources verified; unresolved citations count against it."""
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


def _raw_references(text: str, style: CitationStyle) -> list[dict[str, Any]]:
    """Parse one reference per line; do not guess a source ID from bibliographic text."""
    references: list[dict[str, Any]] = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or re.fullmatch(r"#{1,6}\s*references\s*", entry, re.I):
            continue
        embedded_ids = [match.group(1).strip() for match in _CITE_MARKER.finditer(entry)]
        source_id = embedded_ids[0] if len(set(embedded_ids)) == 1 else None
        rendered = _CITE_MARKER.sub("", entry).strip()
        numbered = re.match(r"^\s*(?:\d+\.\s+)?\[(\d+)\]\s*", rendered)
        if numbered:
            key = numbered.group(1)
        elif style in {CitationStyle.IEEE, CitationStyle.VANCOUVER}:
            numeric = re.match(r"^\s*(\d+)\.\s+", rendered)
            key = numeric.group(1) if numeric else f"reference-{len(references) + 1}"
        else:
            author_date = re.match(
                r"^\s*(?:\d+\.\s*)?([^\s,]+)[^()]*\(((?:18|19|20|21)\d{2}[a-z]?)\)",
                rendered,
                re.I,
            )
            key = (
                f"{author_date.group(1)}, {author_date.group(2)}"
                if author_date
                else f"reference-{len(references) + 1}"
            )
        references.append(
            {"key": key, "text": rendered, "source_id": source_id, "_embedded_ids": embedded_ids}
        )
    return references


class CitationCheckTool:
    """Check machine markers and rendered citations against references and source cards."""

    _NAME: ClassVar[str] = "citation_check"
    _DESCRIPTION: ClassVar[str] = (
        "Check citation orphans, source IDs, duplicates, verification and APA7/IEEE style."
    )
    _TOKEN_COST: ClassVar[int] = 18
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "iter": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "citation_style": {"type": "string", "enum": [style.value for style in CitationStyle]},
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
                "type": ["array", "string"],
                "maxItems": 500,
                "maxLength": 500_000,
                "description": "Structured entries or a one-reference-per-line string.",
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
        raw_references = arguments["references"]
        cards = arguments["source_cards"]
        if not all(isinstance(item, Mapping) for item in (*sections, *cards)):
            return ToolResult(
                tool=self.name, ok=False, content="sections and source_cards must be objects"
            )
        if isinstance(raw_references, str):
            references = _raw_references(raw_references, style)
        elif isinstance(raw_references, list) and all(
            isinstance(item, Mapping) for item in raw_references
        ):
            references = [dict(item) for item in raw_references]
            if not references:
                reference_section = "\n".join(
                    str(section["text"])
                    for section in sections
                    if str(section.get("id", "")).casefold() == "references"
                )
                if reference_section.strip():
                    references = _raw_references(reference_section, style)
        else:
            return ToolResult(
                tool=self.name, ok=False, content="references must be objects or text"
            )
        if len(references) > 500:
            return ToolResult(
                tool=self.name, ok=False, content="at most 500 references are supported"
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
                tool=self.name, ok=False, content="combined citation text exceeds limit"
            )
        marker_ids: list[str] = []
        for match in _CITE_MARKER.finditer(body):
            marker_ids.append(match.group(1).strip())
            if len(marker_ids) > _MAX_CITATION_MARKERS:
                return ToolResult(tool=self.name, ok=False, content="too many citation markers")
        rendered_body = _CITE_MARKER.sub("", body)
        malformed_markers = sum(1 for _ in _CITE_PREFIX.finditer(rendered_body))
        try:
            numeric_markers = _numeric_keys(rendered_body)
            author_date_markers = _apa_keys(rendered_body)
        except ValueError:
            return ToolResult(tool=self.name, ok=False, content="too many citation markers")
        if (
            len(marker_ids) + malformed_markers + len(numeric_markers) + len(author_date_markers)
            > _MAX_CITATION_MARKERS
        ):
            return ToolResult(tool=self.name, ok=False, content="too many citation markers")
        # Even a marker in the wrong style is a citation site: check its graph
        # consistency as well as flagging the style mismatch below.
        cited_keys = [*numeric_markers, *author_date_markers]

        source_ids_by_key: dict[str, str | None] = {}
        ref_keys: list[str] = []
        ref_source_ids: list[str | None] = []
        duplicates: list[str] = []
        ambiguous_refs: set[str] = set()
        seen_keys: set[str] = set()
        seen_ids: set[str] = set()
        seen_dois: set[str] = set()
        seen_texts: set[str] = set()
        for reference in references:
            key = _reference_key(str(reference["key"]))
            normalized = _normal_key(key)
            ref_keys.append(normalized)
            reference_text = str(reference["text"])
            embedded_ids = reference.get("_embedded_ids")
            if not isinstance(embedded_ids, list):
                embedded_ids = [
                    match.group(1).strip() for match in _CITE_MARKER.finditer(reference_text)
                ]
            distinct_embedded = set(embedded_ids)
            source_raw = reference.get("source_id")
            if source_raw is None and len(distinct_embedded) == 1:
                source_raw = next(iter(distinct_embedded))
            if len(distinct_embedded) > 1 or (
                source_raw is not None and distinct_embedded and source_raw not in distinct_embedded
            ):
                ambiguous_refs.add(f"ambiguous source ids for {_label(key)}")
                source_raw = None
            source_id = str(source_raw) if source_raw is not None else None
            ref_source_ids.append(source_id)
            if normalized in source_ids_by_key and source_ids_by_key[normalized] != source_id:
                # An ambiguous rendered key cannot be credited as verified.
                source_ids_by_key[normalized] = None
            else:
                source_ids_by_key.setdefault(normalized, source_id)
            text_norm = _normal_key(_CITE_MARKER.sub("", reference_text))
            doi_match = _DOI_RE.search(reference_text)
            doi_key = doi_match.group(0).rstrip(".").casefold() if doi_match else ""
            if (
                normalized in seen_keys
                or (source_id and source_id in seen_ids)
                or (doi_key and doi_key in seen_dois)
                or (text_norm and text_norm in seen_texts)
            ):
                duplicates.append(key)
            seen_keys.add(normalized)
            if source_id:
                seen_ids.add(source_id)
            if doi_key:
                seen_dois.add(doi_key)
            if text_norm:
                seen_texts.add(text_norm)

        cited_normalized = {_normal_key(key) for key in cited_keys}
        available_refs = set(ref_keys)
        referenced_ids = {source_id for source_id in ref_source_ids if source_id}
        orphan_citations = sorted(cited_normalized - available_refs)
        orphan_citations.extend(
            f"[[CITE:{source_id}]]" for source_id in sorted(set(marker_ids) - referenced_ids)
        )
        if malformed_markers:
            orphan_citations.append("<malformed citation marker>")
        cited_source_ids = set(marker_ids)
        orphan_references = [
            _reference_key(str(reference["key"]))
            for reference, normalized, source_id in zip(
                references, ref_keys, ref_source_ids, strict=True
            )
            if normalized not in cited_normalized and source_id not in cited_source_ids
        ]
        card_status: dict[str, VerificationStatus] = {}
        for card in cards:
            source_id = str(card["id"])
            if not is_ulid(source_id):
                continue
            try:
                status = VerificationStatus(
                    str(card.get("verification", VerificationStatus.UNKNOWN.value))
                )
            except ValueError:
                status = VerificationStatus.UNKNOWN
            if source_id in card_status and card_status[source_id] is not status:
                status = VerificationStatus.UNKNOWN
            card_status[source_id] = status

        unresolved_ids: set[str] = {
            source_id or "<empty citation id>"
            for source_id in marker_ids
            if not is_ulid(source_id) or source_id not in card_status
        }
        for reference, source_id in zip(references, ref_source_ids, strict=True):
            if not source_id:
                unresolved_ids.add(f"missing source id for {_label(str(reference['key']))}")
            elif not is_ulid(source_id) or source_id not in card_status:
                unresolved_ids.add(source_id)
        if malformed_markers:
            unresolved_ids.add("<malformed citation marker>")
        unresolved_ids.update(ambiguous_refs)

        style_violations: list[str] = []
        if marker_ids or malformed_markers:
            style_violations.append(
                "Machine citation markers must be rendered in the requested style."
            )
        if style is CitationStyle.APA7:
            if numeric_markers:
                style_violations.append(
                    "Numeric in-text markers are not APA7 author-date citations."
                )
            for key in author_date_markers:
                if not _APA_CITATION.fullmatch(key):
                    style_violations.append(
                        f"In-text citation {_label(key)} is not APA7 author-date."
                    )
        elif style is CitationStyle.IEEE:
            if author_date_markers:
                style_violations.append(
                    "Author-date in-text markers are not IEEE numeric citations."
                )
            if any(not key.isdigit() for key in numeric_markers):
                style_violations.append(
                    "An IEEE numeric range is malformed or too large to expand."
                )
        else:
            style_violations.append(f"Automated {style.value} style checking is not implemented.")
        for reference in references:
            key = _reference_key(str(reference["key"]))
            text = _CITE_MARKER.sub("", str(reference["text"])).strip()
            if style is CitationStyle.APA7 and not _APA_REFERENCE.search(text):
                style_violations.append(
                    f"Reference {_label(key)} lacks basic APA7 author/date form."
                )
            elif style is CitationStyle.IEEE and (
                not key.isdigit() or not _YEAR.search(text) or not re.search(r"[,.;]", text)
            ):
                style_violations.append(
                    f"Reference {_label(key)} lacks basic IEEE number/year form."
                )

        issues: list[QualityIssue] = []
        issue_count = 0

        def add_issue(message: str, severity: IssueSeverity) -> None:
            nonlocal issue_count
            issue_count += 1
            if len(issues) < _MAX_DIAGNOSTICS:
                issues.append(_issue(issue_count, message, severity))

        for key in orphan_citations:
            add_issue(f"In-text citation {_label(key)} has no reference entry.", IssueSeverity.HIGH)
        for key in orphan_references:
            add_issue(
                f"Reference {_label(key)} is not cited in the article text.", IssueSeverity.MED
            )
        for key in duplicates:
            add_issue(
                f"Reference entry {_label(key)} duplicates another reference.", IssueSeverity.MED
            )
        for source_id in sorted(unresolved_ids):
            add_issue(f"Unresolved source id: {_label(source_id)}.", IssueSeverity.HIGH)
        for source_id in sorted(referenced_ids - unresolved_ids):
            reference_status = card_status.get(source_id)
            if reference_status is not None and reference_status is not VerificationStatus.VERIFIED:
                add_issue(
                    f"Reference source {source_id} is {reference_status.value}; "
                    "validator verification is required.",
                    IssueSeverity.MED,
                )
        for violation in style_violations:
            add_issue(violation, IssueSeverity.MED)

        ratio_ids = {source_id for source_id in marker_ids if source_id}
        unresolved_citation_keys: set[str] = set()
        if "" in marker_ids:
            unresolved_citation_keys.add("<empty citation id>")
        if malformed_markers:
            unresolved_citation_keys.add("<malformed citation marker>")
        for key in cited_keys:
            normalized = _normal_key(key)
            source_id = source_ids_by_key.get(normalized)
            if source_id:
                ratio_ids.add(source_id)
            else:
                unresolved_citation_keys.add(normalized)
        denominator = len(ratio_ids) + len(unresolved_citation_keys)
        verified = sum(
            card_status.get(source_id) is VerificationStatus.VERIFIED for source_id in ratio_ids
        )
        ratio = verified / denominator if denominator else 0.0
        gates = dict.fromkeys(GATE_NAMES, False)
        gates["citations_resolve_both_ways"] = not (
            orphan_citations or orphan_references or duplicates or unresolved_ids
        )
        gates["references_verified_or_flagged"] = issue_count <= _MAX_DIAGNOSTICS
        gates["citation_style_matches"] = not style_violations
        omitted_issues = (
            issue_count - (_MAX_DIAGNOSTICS - 1) if issue_count > _MAX_DIAGNOSTICS else 0
        )
        report_issues = issues[: _MAX_DIAGNOSTICS - 1] if omitted_issues else issues
        if omitted_issues:
            report_issues.append(
                _issue(
                    _MAX_DIAGNOSTICS,
                    f"{omitted_issues} additional citation issue(s) were summarized.",
                    IssueSeverity.MED,
                )
            )
        report = QualityReport(
            iter=int(arguments["iter"]),
            gates=gates,
            Q=0,
            verified_citation_ratio=ratio,
            issues=report_issues,
        )
        diagnostics = {
            "orphan_citations": orphan_citations,
            "orphan_references": orphan_references,
            "unresolved_ids": sorted(unresolved_ids),
            "duplicate_references": duplicates,
            "style_violations": style_violations,
        }
        counts = {key: len(values) for key, values in diagnostics.items()}
        payload: dict[str, Any] = {
            "report": report.model_dump(mode="json"),
            "assessed_gates": [
                "citations_resolve_both_ways",
                "references_verified_or_flagged",
                "citation_style_matches",
            ],
            "citation_count": len(cited_keys) + len(marker_ids) + malformed_markers,
            "reference_count": len(references),
            "verified_citation_ratio": ratio,
            "diagnostic_counts": counts,
            "omitted_issue_count": omitted_issues,
            "diagnostics_truncated": bool(omitted_issues)
            or any(count > _MAX_DIAGNOSTICS for count in counts.values()),
            **{key: values[:_MAX_DIAGNOSTICS] for key, values in diagnostics.items()},
        }
        # Keep the JSON envelope valid under the default registry cap. Counts and
        # gates always reflect the entire input, even when diagnostics are shortened.
        while True:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content) <= _MAX_RESULT_CHARS:
                break
            lists = [payload[key] for key in diagnostics if payload[key]]
            if lists:
                max(lists, key=lambda values: len(json.dumps(values, ensure_ascii=False))).pop()
            elif payload["report"]["issues"]:
                payload["report"]["issues"].pop()
                payload["report"]["gates"]["references_verified_or_flagged"] = False
                payload["omitted_issue_count"] += 1
            else:
                break
            payload["diagnostics_truncated"] = True
        return ToolResult(
            tool=self.name, content=content, truncated=bool(payload["diagnostics_truncated"])
        )
