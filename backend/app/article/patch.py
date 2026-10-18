"""Apply and validate the ``Patch`` contract.

This module is pure: it takes the current body of one section plus a ``Patch`` and
returns the new body with diff statistics. It never touches storage, never bumps a
version and never waits on anything - the store owns those concerns. Keeping the
rules here means the reject matrix is testable without a database and identical for
every caller.

Two failure modes, deliberately distinct:

* :class:`StalePatch` - the section moved on since the agent read it. Nothing is
  wrong with the patch; it must be re-authored against the new ``base_version``.
* :class:`PatchRejected` - the patch does not describe this text. The exception
  carries one :class:`PatchProblem` per offending op, with a bounded repair hint,
  so a single repair round trip can fix the whole patch (HARD RULE 3).

Matching is literal and exact: ``old`` and ``anchor`` must occur exactly once. When
they do not, the reason says how many times they occurred (or what the closest
stored line is), because a bare "not found" costs another full round trip.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from typing import Final, Literal

from app.contracts.common import word_count
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import Patch, PatchOp
from app.core.errors import ContractViolation, ScholarLoopError
from app.core.hashing import content_hash, normalise_text

__all__ = [
    "REPLACE_SECTION_ALLOWED_STATUSES",
    "DiffStats",
    "PatchError",
    "PatchOutcome",
    "PatchProblem",
    "PatchRejected",
    "StalePatch",
    "TextDiff",
    "apply_ops",
    "diff_stats",
    "diff_texts",
    "validate_ops",
]

#: ``replace_section`` may only rewrite a section nobody has signed off on yet.
REPLACE_SECTION_ALLOWED_STATUSES: Final[frozenset[SectionStatus]] = frozenset(
    {SectionStatus.EMPTY, SectionStatus.DRAFT}
)
#: Reasons travel back into a prompt; they stay short enough to be cheap.
MAX_REASON_SNIPPET: Final[int] = 80
#: Hard ceiling on one rendered problem line.
MAX_PROBLEM_CHARS: Final[int] = 320
#: Below this similarity a "closest line" hint is noise, not help.
CLOSE_LINE_RATIO: Final[float] = 0.6

PayloadField = Literal["anchor", "old", "new", "text"]
#: How a matched span is rewritten.
EditMode = Literal["replace", "insert_after", "delete"]

_COLLAPSE_RE = re.compile(r"\s+")


class PatchError(ScholarLoopError):
    """Base class for every patch failure the store can surface."""

    code = "patch_error"
    http_status = 422


class StalePatch(PatchError):
    """``patch.base_version`` no longer matches the section's version.

    Optimistic concurrency working as intended: the patch is discarded rather than
    merged, and the agent re-reads the section before trying again.
    """

    code = "stale_patch"
    http_status = 409

    def __init__(self, *, section_id: str, base_version: int, current_version: int) -> None:
        self.section_id = section_id
        self.base_version = base_version
        self.current_version = current_version
        super().__init__(
            f"patch for section '{section_id}' was authored against v{base_version} but the "
            f"section is at v{current_version}; re-read the section and re-issue the patch",
            section_id=section_id,
            base_version=base_version,
            current_version=current_version,
        )


@dataclass(frozen=True)
class PatchProblem:
    """One precise, repairable complaint about one op."""

    op_index: int
    op: PatchOpType
    reason: str
    hint: str

    def render(self, *, limit: int = MAX_PROBLEM_CHARS) -> str:
        """Reason plus hint, truncated to a bounded length."""
        rendered = f"{self.reason} {self.hint}".strip()
        return rendered if len(rendered) <= limit else f"{rendered[: limit - 1]}…"


class PatchRejected(PatchError):
    """The patch does not describe the section's current text.

    ``problems`` holds *every* problem found (validation runs to completion), so the
    agent gets one complete repair instruction instead of one error per round trip.
    """

    code = "patch_rejected"
    http_status = 422

    def __init__(
        self, problems: Sequence[PatchProblem], *, section_id: str, base_version: int
    ) -> None:
        self.problems: tuple[PatchProblem, ...] = tuple(problems)
        self.section_id = section_id
        self.base_version = base_version
        first = self.problems[0]
        super().__init__(
            f"patch rejected for section '{section_id}': {first.render()}",
            section_id=section_id,
            base_version=base_version,
            op_index=first.op_index,
            op=first.op.value,
            reason=first.reason,
            hint=first.hint,
            problems=[problem.render() for problem in self.problems],
        )

    @property
    def op_index(self) -> int:
        """Index of the first offending op."""
        return self.problems[0].op_index

    def repair_instruction(self) -> str:
        """Everything the agent needs to fix the patch, in one bounded string."""
        header = (
            f"Patch rejected for section '{self.section_id}' "
            f"(authored against v{self.base_version}). Fix {len(self.problems)} op(s):"
        )
        lines = [header]
        budget = MAX_PROBLEM_CHARS * max(1, len(self.problems))
        total = len(header)
        for problem in self.problems:
            line = problem.render()
            if total + len(line) + 1 > budget:
                lines.append("…further problems omitted; re-read the section before repairing.")
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)


@dataclass(frozen=True)
class DiffStats:
    """Character-level change size, for the ledger and the erosion detectors."""

    chars_added: int
    chars_removed: int

    @property
    def net_chars(self) -> int:
        """Growth (negative when the patch shortened the section)."""
        return self.chars_added - self.chars_removed

    @property
    def touched_chars(self) -> int:
        """Total churn: how much text the patch moved, in either direction."""
        return self.chars_added + self.chars_removed


@dataclass(frozen=True)
class PatchOutcome:
    """Result of applying a patch to a body of text.

    ``text`` is normalised (see :func:`app.core.hashing.normalise_text`) - that is
    what gets stored and hashed. ``changed`` is False when the patch only altered
    whitespace, so the store can keep the old version instead of inventing progress.
    """

    section_id: str
    base_version: int
    text: str
    hash: str
    words: int
    stats: DiffStats
    ops_applied: int
    changed: bool


def diff_stats(old: str, new: str) -> DiffStats:
    """Characters added and removed between two bodies of text."""
    matcher = SequenceMatcher(a=old, b=new, autojunk=False)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return DiffStats(chars_added=added, chars_removed=removed)


@dataclass(frozen=True)
class TextDiff:
    """A rendered diff between two bodies, for ``store.diff`` and the API."""

    stats: DiffStats
    lines_added: int
    lines_removed: int
    unified: str


def diff_texts(
    old: str,
    new: str,
    *,
    from_label: str = "before",
    to_label: str = "after",
    context: int = 3,
) -> TextDiff:
    """Character stats plus a unified diff. The only diff renderer in the package."""
    lines = list(
        unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            n=context,
            lineterm="",
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return TextDiff(
        stats=diff_stats(old, new),
        lines_added=added,
        lines_removed=removed,
        unified="\n".join(lines),
    )


def apply_ops(text: str, patch: Patch, *, status: SectionStatus) -> PatchOutcome:
    """Apply every op of ``patch`` to ``text``, in order.

    All-or-nothing: any problem raises :class:`PatchRejected` and the caller keeps
    the original body. Ops are applied sequentially, so a later op addresses the
    text the earlier ops produced.

    Raises:
        PatchRejected: an op fails to match, or ``replace_section`` targets a section
            that is already ``reviewed`` or ``final``.
    """
    outcome, problems = _run(text, patch, status=status)
    if problems:
        raise PatchRejected(problems, section_id=patch.section_id, base_version=patch.base_version)
    return outcome


def validate_ops(text: str, patch: Patch, *, status: SectionStatus) -> tuple[PatchProblem, ...]:
    """Dry run: every problem :func:`apply_ops` would raise, without raising.

    Lets an agent self-check a patch before it leaves the process, so a broken patch
    costs no tokens on the way back.
    """
    _, problems = _run(text, patch, status=status)
    return problems


@dataclass(frozen=True)
class _OpResult:
    """Exactly one of: the body after this op, or why the op cannot be applied."""

    body: str | None = None
    problem: PatchProblem | None = None


def _run(
    text: str, patch: Patch, *, status: SectionStatus
) -> tuple[PatchOutcome, tuple[PatchProblem, ...]]:
    """Shared engine for :func:`apply_ops` and :func:`validate_ops`."""
    original = normalise_text(text)
    body = original
    problems: list[PatchProblem] = []
    applied = 0

    for index, op in enumerate(patch.ops):
        result = _apply_one(body, op, index=index, section_id=patch.section_id, status=status)
        if result.problem is not None:
            problems.append(result.problem)
        elif result.body is not None:
            body = result.body
            applied += 1

    body = normalise_text(body)
    outcome = PatchOutcome(
        section_id=patch.section_id,
        base_version=patch.base_version,
        text=body,
        hash=content_hash(body),
        words=word_count(body),
        stats=diff_stats(original, body),
        ops_applied=applied,
        changed=content_hash(body) != content_hash(original),
    )
    return outcome, tuple(problems)


def _apply_one(
    body: str,
    op: PatchOp,
    *,
    index: int,
    section_id: str,
    status: SectionStatus,
) -> _OpResult:
    """Apply one op verb. One branch per verb, each a splice with a stated rule."""
    if op.op is PatchOpType.REPLACE_SECTION:
        if status not in REPLACE_SECTION_ALLOWED_STATUSES:
            return _OpResult(problem=_replace_section_problem(index, section_id, status))
        return _OpResult(body=normalise_text(_payload(op, "text")))

    if op.op is PatchOpType.INSERT_AFTER:
        return _splice(
            body,
            needle=_payload(op, "anchor"),
            payload=_payload(op, "text"),
            mode="insert_after",
            field="anchor",
            op=op,
            index=index,
            section_id=section_id,
        )

    if op.op is PatchOpType.DELETE_TEXT:
        return _splice(
            body,
            needle=_payload(op, "old"),
            payload="",
            mode="delete",
            field="old",
            op=op,
            index=index,
            section_id=section_id,
        )

    return _splice(
        body,
        needle=_payload(op, "old"),
        payload=_payload(op, "new"),
        mode="replace",
        field="old",
        op=op,
        index=index,
        section_id=section_id,
    )


def _replace_section_problem(index: int, section_id: str, status: SectionStatus) -> PatchProblem:
    """``replace_section`` against a section that is already signed off."""
    allowed = " or ".join(sorted(state.value for state in REPLACE_SECTION_ALLOWED_STATUSES))
    return PatchProblem(
        op_index=index,
        op=PatchOpType.REPLACE_SECTION,
        reason=(
            f"op[{index}] replace_section: section '{section_id}' is '{status.value}'; "
            f"replace_section is allowed only when the section is {allowed}"
        ),
        hint=(
            "Express the change as replace_text / insert_after / delete_text, "
            "or demote the section to 'draft' first."
        ),
    )


def _splice(
    body: str,
    *,
    needle: str,
    payload: str,
    mode: EditMode,
    field: PayloadField,
    op: PatchOp,
    index: int,
    section_id: str,
) -> _OpResult:
    """Require ``needle`` to occur exactly once, then splice ``payload`` in."""
    offsets = _occurrences(body, needle)
    if len(offsets) != 1:
        return _OpResult(
            problem=PatchProblem(
                op_index=index,
                op=op.op,
                reason=(
                    f"op[{index}] {op.op.value}: '{field}' occurs {len(offsets)} time(s) in "
                    f"section '{section_id}' and must occur exactly once"
                    + (f" (at chars {', '.join(str(o) for o in offsets[:3])})" if offsets else "")
                ),
                hint=_hint_for(body, needle, field=field, occurrences=len(offsets)),
            )
        )

    start = offsets[0]
    end = start + len(needle)
    if mode == "delete":
        return _OpResult(body=body[:start] + body[end:])
    if mode == "insert_after":
        return _OpResult(body=body[:end] + payload + body[end:])
    return _OpResult(body=body[:start] + payload + body[end:])


def _hint_for(body: str, needle: str, *, field: PayloadField, occurrences: int) -> str:
    """A bounded, actionable hint: why the match failed and what to send instead."""
    if occurrences > 1:
        return f"Widen '{field}' with surrounding words until the match is unique."
    collapsed_body = _COLLAPSE_RE.sub(" ", body).strip()
    collapsed_needle = _COLLAPSE_RE.sub(" ", needle).strip()
    if collapsed_needle and collapsed_body.count(collapsed_needle) == 1:
        return (
            f"The text occurs once when whitespace is collapsed, so your '{field}' differs from "
            "the stored body in line breaks or spacing. Copy the exact bytes from the section."
        )
    closest = _closest_line(body, needle)
    if closest is not None:
        return (
            f"Closest stored line: {_truncate(closest)!r}. Copy it verbatim, whitespace included."
        )
    return (
        f"'{field}' is absent from the section. Re-read the section (version and body) and "
        "re-issue the patch."
    )


def _occurrences(text: str, needle: str) -> list[int]:
    """All (possibly overlapping) start offsets of ``needle`` in ``text``."""
    if not needle:
        return []
    offsets: list[int] = []
    start = text.find(needle)
    while start != -1:
        offsets.append(start)
        start = text.find(needle, start + 1)
    return offsets


def _closest_line(body: str, needle: str) -> str | None:
    """The stored line most similar to ``needle``, when the match is close enough."""
    target = _COLLAPSE_RE.sub(" ", needle).strip()
    if not target:
        return None
    best_line: str | None = None
    best_ratio = 0.0
    for line in body.split("\n"):
        candidate = _COLLAPSE_RE.sub(" ", line).strip()
        if not candidate:
            continue
        ratio = SequenceMatcher(a=target, b=candidate, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = line.strip()
    if best_line is None or best_ratio < CLOSE_LINE_RATIO:
        return None
    return best_line


def _truncate(value: str, *, limit: int = MAX_REASON_SNIPPET) -> str:
    """Keep a snippet inside the reason budget."""
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _payload(op: PatchOp, field: PayloadField) -> str:
    """Read a required op field. The contract guarantees it; this keeps mypy honest."""
    value = getattr(op, field)
    if not isinstance(value, str) or not value:
        raise ContractViolation(
            f"op '{op.op.value}' is missing required field '{field}'",
            op=op.op.value,
            field=field,
        )
    return value
