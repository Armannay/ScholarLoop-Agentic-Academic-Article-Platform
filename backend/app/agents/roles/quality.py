"""The validator's deterministic half: hard gates, score merge, weighted Q.

The validator is the one role allowed to publish a ``QualityReport``, and the report is
the loop's stop condition. So the parts a model could get wrong are computed here:

* **Hard gates come from tools.** ``text_checks`` reports four of them,
  ``citation_check`` reports three, and the eighth (figures and tables) is a local regex
  check. The model is never asked for a gate, and a gate a tool did not assess stays
  ``False`` with an issue explaining why - an absent gate must never read as a pass.
* **Q comes from the reviewer.** The reviewer scores each dirty section against the
  rubric; this module averages those per criterion and *carries a score forward* only
  while the section hash still matches the text that was scored. No fresh score and no
  earlier report means the criterion sits at :data:`BASELINE_SCORE`, which is honest:
  an unscored criterion is average, not excellent.
* **``verified_citation_ratio`` comes from ``citation_check``**, computed from the same
  card statuses the report quotes.

The arithmetic is intentionally boring: a weighted mean with the rubric's weights, an
``int`` result, and no adjustment towards any threshold (the loop owns thresholds).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.agents.roles.inputs import ReviewScoreboard
from app.contracts.brief import Brief
from app.contracts.enums import AgentRole, IssueSeverity, SectionStatus, VerificationStatus
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.core.rubric import Rubric
from app.tools.base import ToolRegistry

__all__ = [
    "BASELINE_SCORE",
    "CITATION_CHECK_GATES",
    "FIGURE_GATE",
    "TEXT_CHECK_GATES",
    "GatePack",
    "figure_and_table_gate",
    "merge_scores",
    "run_deterministic_gates",
    "section_states_after_gates",
    "weighted_q",
]

#: An unscored criterion is average. Never invent excellence for unmeasured work.
BASELINE_SCORE: Final[int] = 50
#: Gates ``text_checks`` assesses (see app/tools/text_checks.py).
TEXT_CHECK_GATES: Final[tuple[str, ...]] = (
    "required_sections_present",
    "abstract_within_range",
    "total_words_within_tolerance",
    "no_placeholders",
)
#: Gates ``citation_check`` assesses (see app/tools/citation_check.py).
CITATION_CHECK_GATES: Final[tuple[str, ...]] = (
    "citations_resolve_both_ways",
    "references_verified_or_flagged",
    "citation_style_matches",
)
#: The one gate neither tool covers: it is a local, deterministic text scan.
FIGURE_GATE: Final[str] = "referenced_figures_and_tables_exist"

_FIGURE_REF = re.compile(r"(?i)\b(?:see\s+)?(?:fig(?:ure)?\.?|table)\s*[~-]?\s*(\d+[a-z]?)")
_FIGURE_DEF = re.compile(r"(?im)^\s*(?:\*\*|#{1,6}\s*)?(?:fig(?:ure)?\.?|table)\s*(\d+[a-z]?)\b")
_MAX_TOOL_ISSUES: Final[int] = 200


@dataclass(frozen=True)
class GatePack:
    """Everything the validator learned before it asked a model anything."""

    gates: dict[str, bool]
    verified_citation_ratio: float
    issues: list[QualityIssue] = field(default_factory=list)
    assessed_by: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    updated_sources: list[SourceCard] = field(default_factory=list)

    @property
    def all_gates_pass(self) -> bool:
        """True when every hard gate is reported as passing."""
        return all(self.gates.get(name, False) for name in GATE_NAMES)

    @property
    def failed_gates(self) -> list[str]:
        """Failed gate names, in canonical order."""
        return [name for name in GATE_NAMES if not self.gates.get(name, False)]

    def prompt_summary(self) -> str:
        """A compact, deterministic summary of the pack for the model's prompt."""
        payload = {
            "gates": {name: bool(self.gates.get(name, False)) for name in GATE_NAMES},
            "assessed_by": dict(sorted(self.assessed_by.items())),
            "verified_citation_ratio": round(self.verified_citation_ratio, 4),
            "failed_gates": self.failed_gates,
            "open_issues": [
                {
                    "id": issue.id,
                    "section_id": issue.section_id,
                    "severity": issue.severity.value,
                    "text": issue.text,
                }
                for issue in self.issues[:20]
            ],
            "notes": list(self.notes),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------------------ gates
def figure_and_table_gate(texts: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Every referenced figure/table label must also be defined somewhere.

    A reference with no definition is the failure the gate exists for ("as Figure 3
    shows" when there is no Figure 3). A definition with no reference is normal in a
    draft and is left to the reviewer; the gate is about dangling references.
    """
    body = "\n".join(texts.values())
    referenced = {label.lower() for label in _FIGURE_REF.findall(body)}
    defined = {label.lower() for label in _FIGURE_DEF.findall(body)}
    missing = sorted(referenced - defined)
    return (not missing, missing)


def _issue(
    prefix: str, text: str, *, severity: IssueSeverity, section_id: str | None = None
) -> QualityIssue:
    return QualityIssue(
        id=f"ISSUE-{prefix}"[:40], section_id=section_id, severity=severity, text=text[:600]
    )


def _parse_tool_payload(content: str) -> dict[str, Any]:
    """Parse a tool result's JSON object; an unreadable result is an empty mapping."""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def _invoke(
    tools: ToolRegistry | None,
    name: str,
    arguments: Mapping[str, Any],
    *,
    project: str | None,
) -> dict[str, Any] | None:
    """Invoke a validator tool if it is registered and granted; ``None`` otherwise."""
    if tools is None:
        return None
    if name not in tools.names_for(AgentRole.VALIDATOR, project=project):
        return None
    result = await tools.invoke(name, arguments, role=AgentRole.VALIDATOR, project=project)
    if not result.ok:
        return None
    return _parse_tool_payload(result.content)


def _tool_gates(payload: Mapping[str, Any]) -> dict[str, bool]:
    """Gates a tool reported, ignoring anything it did not actually assess."""
    report = payload.get("report")
    gates = report.get("gates") if isinstance(report, Mapping) else None
    assessed = payload.get("assessed_gates")
    names = (
        [str(name) for name in assessed] if isinstance(assessed, list) else list(TEXT_CHECK_GATES)
    )
    if not isinstance(gates, Mapping):
        return {}
    return {name: bool(gates.get(name, False)) for name in names}


def _tool_issues(payload: Mapping[str, Any]) -> list[QualityIssue]:
    """Issues a tool reported, converted to the contract shape."""
    report = payload.get("report")
    raw = report.get("issues") if isinstance(report, Mapping) else None
    if not isinstance(raw, list):
        return []
    issues: list[QualityIssue] = []
    for item in raw[:_MAX_TOOL_ISSUES]:
        try:
            issues.append(QualityIssue.model_validate(item))
        except ValueError:
            continue
    return issues


async def run_deterministic_gates(
    *,
    tools: ToolRegistry | None,
    texts: Mapping[str, str],
    brief: Brief,
    iteration: int,
    sources: Sequence[SourceCard],
    project: str | None = None,
) -> GatePack:
    """Run the deterministic checks and assemble the hard-gate pack.

    Order matters: DOI verification runs first so citation checking sees the updated
    ``verification`` statuses, which is what makes ``verified_citation_ratio`` and the
    ``references_verified_or_flagged`` gate describe the same facts.
    """
    gates: dict[str, bool] = dict.fromkeys(GATE_NAMES, False)
    assessed_by: dict[str, str] = {}
    notes: list[str] = []
    issues: list[QualityIssue] = []
    updated_sources: list[SourceCard] = []
    ratio = 0.0

    for card in sources:
        if card.doi is None:
            continue
        payload = await _invoke(
            tools, "doi_verify", {"source": card.model_dump(mode="json")}, project=project
        )
        if payload is None:
            continue
        update = payload.get("source_update")
        if not isinstance(update, Mapping):
            continue
        try:
            status = VerificationStatus(str(update.get("verification")))
        except ValueError:
            continue
        if status is not card.verification:
            updated_sources.append(card.model_copy(update={"verification": status}))

    card_payload = [
        {"id": card.id, "verification": card.verification.value}
        for card in (*sources, *updated_sources)
    ]
    sections_payload = [{"id": section_id, "text": text} for section_id, text in texts.items()]

    text_payload = await _invoke(
        tools,
        "text_checks",
        {
            "iter": iteration,
            "article_type": brief.article_type.value,
            "target_words": brief.target_words,
            "sections": sections_payload,
        },
        project=project,
    )
    if text_payload is None:
        for name in TEXT_CHECK_GATES:
            notes.append(f"{name}: text_checks unavailable, gate cannot pass unassessed")
    else:
        for name, passed in _tool_gates(text_payload).items():
            gates[name] = passed
            assessed_by[name] = "text_checks"
        issues.extend(_tool_issues(text_payload))

    references_text = texts.get("references", "")
    citation_payload = await _invoke(
        tools,
        "citation_check",
        {
            "iter": iteration,
            "citation_style": brief.citation_style.value,
            "sections": sections_payload,
            "references": references_text,
            "source_cards": card_payload,
        },
        project=project,
    )
    if citation_payload is None:
        for name in CITATION_CHECK_GATES:
            notes.append(f"{name}: citation_check unavailable, gate cannot pass unassessed")
    else:
        for name, passed in _tool_gates(citation_payload).items():
            gates[name] = passed
            assessed_by[name] = "citation_check"
        raw_ratio = citation_payload.get("verified_citation_ratio")
        if isinstance(raw_ratio, (int, float)):
            ratio = min(1.0, max(0.0, float(raw_ratio)))
        issues.extend(_tool_issues(citation_payload))

    figures_ok, missing_figures = figure_and_table_gate(texts)
    gates[FIGURE_GATE] = figures_ok
    assessed_by[FIGURE_GATE] = "figure_scan"
    for label in missing_figures:
        issues.append(
            _issue(
                f"figure-{label}",
                f"Figure/table {label} is referenced but never defined.",
                severity=IssueSeverity.HIGH,
            )
        )

    for name in GATE_NAMES:
        if name not in assessed_by:
            notes.append(f"{name}: not assessed by any tool, reported as failed")

    return GatePack(
        gates={name: bool(gates.get(name, False)) for name in GATE_NAMES},
        verified_citation_ratio=ratio,
        issues=issues,
        assessed_by=assessed_by,
        notes=notes,
        updated_sources=updated_sources,
    )


# ----------------------------------------------------------------------------- scores
def _clamp(value: int) -> int:
    return max(0, min(100, value))


def merge_scores(
    *,
    rubric: Rubric,
    sections: Sequence[SectionState],
    scoreboard: ReviewScoreboard,
    previous: QualityReport | None = None,
) -> dict[str, int]:
    """Per-criterion scores for Q: fresh reviewer scores, else carried forward.

    A reviewer score counts only when the hash it was recorded against still matches the
    section's current hash. A section that changed since it was reviewed contributes
    nothing, and its criterion falls back to the last report's value - the reviewer will
    score it again next pass.
    """
    criteria = [criterion.name for criterion in rubric.criteria]
    latest = scoreboard.latest()
    fresh: dict[str, list[int]] = {name: [] for name in criteria}
    for state in sections:
        entry = latest.get(state.id)
        if entry is None or entry.section_hash != state.hash:
            continue
        for name, value in entry.scores.items():
            if name in fresh:
                fresh[name].append(int(value))

    carried = dict(previous.scores) if previous is not None else {}
    merged: dict[str, int] = {}
    for name in criteria:
        values = fresh[name]
        if values:
            merged[name] = _clamp(round(sum(values) / len(values)))
        elif name in carried:
            merged[name] = _clamp(int(carried[name]))
        else:
            merged[name] = BASELINE_SCORE
    return merged


def weighted_q(scores: Mapping[str, int], rubric: Rubric) -> int:
    """The rubric's weighted mean of the soft scores, rounded to an int.

    Missing criteria are ignored rather than defaulted here: :func:`merge_scores`
    already guarantees one value per criterion, and silently inventing a weight for an
    unknown key would make ``Q`` unauditable.
    """
    weights = rubric.weights()
    total = 0.0
    weight_sum = 0.0
    for name, weight in weights.items():
        if name not in scores:
            continue
        total += float(scores[name]) * weight
        weight_sum += weight
    if weight_sum == 0.0:
        return 0
    return _clamp(round(total / weight_sum))


def section_states_after_gates(
    *,
    sections: Sequence[SectionState],
    pack: GatePack,
    high_issue_sections: set[str],
) -> list[SectionState]:
    """Sections as the validator leaves them: ``final`` only when it is defensible.

    A section becomes ``final`` when every hard gate passes, no high-severity issue is
    open against it, and it has text (version >= 1). Everything else keeps the status it
    had, so a draft stays a draft and nobody has to trust a verdict this pass did not
    earn.
    """
    promoted: list[SectionState] = []
    for state in sections:
        eligible = (
            pack.all_gates_pass
            and state.version > 0
            and state.id not in high_issue_sections
            and state.status is not SectionStatus.FINAL
        )
        promoted.append(
            state.model_copy(update={"status": SectionStatus.FINAL}) if eligible else state
        )
    return promoted
