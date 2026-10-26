"""Run persistence: the serialisable ``RunRecord`` and its stores.

Step 9b, item 2 (the persistence half). A ``RunRecord`` is everything needed
to resume a run after a crash: the brief, the plan, the ledger entries, the
quality reports, the loop decisions, the event log, the source cards and a
full dump of the article store (section heads, every version, every
snapshot). The orchestrator rehydrates its in-memory stores from the record
and re-dumps them after every applied task, so the record on disk is never
more than one task behind the truth.

Two stores implement :class:`RunStore`:

* :class:`InMemoryRunStore` - deep copies in a dict. Tests and same-process
  crash simulation (a second orchestrator over the same store object).
* :class:`FileRunStore` - one JSON file per run, written atomically
  (``.tmp`` + ``os.replace``) so a crash mid-write can never tear a record.
  This is the dev/prod default; a SQL store can implement the same protocol
  later without touching the orchestrator.

No secrets ever enter a record: briefs, entries and events carry ids and
summaries only (HARD RULE 2).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from app.contracts.brief import Brief
from app.contracts.common import Contract, Ulid, VersionedContract
from app.contracts.enums import SectionStatus
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.contracts.verdict import Verdict
from app.core.clock import utc_now
from app.orchestrator.errors import OrchestratorError, RunNotFound
from app.orchestrator.intake import IntakeKind
from app.orchestrator.state_machine import CheckpointKind, RunEvent, RunStateName
from app.orchestrator.stop import OnComplete, StopReport

__all__ = [
    "ArticleDump",
    "FileRunStore",
    "InMemoryRunStore",
    "RunRecord",
    "RunStore",
    "RunStoreError",
    "SectionDump",
    "SnapshotDump",
    "SnapshotSectionDump",
    "VersionDump",
]


class RunStoreError(OrchestratorError):
    """A run record could not be read or written."""

    code = "run_store_error"


# --------------------------------------------------------------------------- article dump
class _TextDump(Contract):
    """Base for dumps carrying article bodies: empty text is legal and the
    exact bytes matter, so no min-length and no whitespace stripping."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, str_strip_whitespace=False, str_min_length=0
    )


class SectionDump(_TextDump):
    """One section head: contract state plus body (``StoredSection`` as data)."""

    state: SectionState
    text: str
    updated_at: datetime


class VersionDump(_TextDump):
    """One immutable section version (``StoredVersion`` as data)."""

    section_id: str
    version: int = Field(ge=0)
    hash: str
    words: int = Field(ge=0)
    text: str
    status: SectionStatus
    patch_id: str | None = None
    entry_id: str | None = None
    chars_added: int = 0
    chars_removed: int = 0
    created_at: datetime
    q_after: float | None = None


class SnapshotSectionDump(_TextDump):
    """One section frozen in a snapshot."""

    state: SectionState
    text: str


class SnapshotDump(Contract):
    """One whole-article snapshot (``Snapshot`` as data)."""

    id: str
    label: str
    q: float | None = None
    iter: int | None = None
    created_at: datetime
    sections: list[SnapshotSectionDump] = Field(default_factory=list)


class ArticleDump(Contract):
    """The complete article store, serialised for crash resume."""

    sections: list[SectionDump] = Field(default_factory=list)
    versions: list[VersionDump] = Field(default_factory=list)
    snapshots: list[SnapshotDump] = Field(default_factory=list)


def _zero_usage() -> TokenUsage:
    return TokenUsage(in_=0, out=0, cached=0)


# --------------------------------------------------------------------------- record
class RunRecord(VersionedContract):
    """One run, fully serialisable. This is the crash-resume unit.

    ``entries`` mirrors the append-only ledger; ``decisions`` mirrors the
    loop-controller output per iteration; ``strikes`` is the soft-detector
    strike table the controller reads back. Budget extensions granted by the
    user at checkpoints (``extra_token_budget``, ``extra_iterations``) live
    here so caps survive a restart too.
    """

    id: Ulid
    project_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, max_length=64)
    kind: IntakeKind = IntakeKind.BRIEF
    state: RunStateName = RunStateName.INTAKE
    brief: Brief | None = None
    quick_edit_task: Task | None = None
    plan: Plan | None = None
    goal_override: str | None = Field(default=None, max_length=800)
    iter: int = Field(default=0, ge=0, le=10_000)
    entries: list[ChangeEntry] = Field(default_factory=list)
    reports: list[QualityReport] = Field(default_factory=list)
    decisions: list[LoopDecision] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    sources: list[SourceCard] = Field(default_factory=list)
    article: ArticleDump = Field(default_factory=ArticleDump)
    planner_tokens: TokenUsage = Field(default_factory=_zero_usage)
    task_attempts: dict[str, int] = Field(default_factory=dict)
    strikes: dict[str, int] = Field(default_factory=dict)
    on_complete: OnComplete = OnComplete.ASK
    continue_once_used: bool = False
    checkpoint_kind: CheckpointKind | None = None
    user_stop_requested: bool = False
    user_notes: list[str] = Field(default_factory=list, max_length=20)
    extra_token_budget: int = Field(default=0, ge=0)
    extra_iterations: int = Field(default=0, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0.0)
    wall_seconds: float = Field(default=0.0, ge=0.0)
    best_snapshot_id: str | None = None
    best_q: float | None = None
    unverified_by_iter: dict[str, int] = Field(
        default_factory=dict,
        description="Unverified citation count at each iteration end (integrity drift).",
    )
    verdict: Verdict | None = None
    verdict_iter: int | None = None
    stop_report: StopReport | None = None
    last_error: str | None = Field(default=None, max_length=400)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # -- derived views ------------------------------------------------------
    def agent_tokens(self) -> TokenUsage:
        """Summed token usage of every ledger entry."""
        total = _zero_usage()
        for entry in self.entries:
            total = TokenUsage(
                in_=total.in_ + entry.tokens.in_,
                out=total.out + entry.tokens.out,
                cached=total.cached + entry.tokens.cached,
            )
        return total

    def total_tokens_spent(self) -> int:
        """Run tokens across agents and the planner (what the caps measure)."""
        return self.agent_tokens().total + self.planner_tokens.total

    def q_series(self) -> list[int]:
        """Q per reported iteration, in order."""
        return [report.Q for report in self.reports]

    def public_dict(self) -> dict[str, Any]:
        """JSON-safe view for the API (no article bodies at this level)."""
        return self.model_dump(mode="json", by_alias=True, exclude={"article"})


# --------------------------------------------------------------------------- stores
@runtime_checkable
class RunStore(Protocol):
    """Persistence port for run records."""

    def save(self, record: RunRecord) -> None:
        """Insert or replace ``record`` (keyed by ``record.id``)."""
        ...

    def load(self, run_id: str) -> RunRecord:
        """Return the record for ``run_id``; raise :class:`RunNotFound`."""
        ...

    def get(self, run_id: str) -> RunRecord | None:
        """Return the record for ``run_id``, or ``None``."""
        ...

    def list_ids(self) -> list[str]:
        """Every stored run id, sorted."""
        ...

    def delete(self, run_id: str) -> bool:
        """Remove a record; True when something was deleted."""
        ...


class InMemoryRunStore:
    """Dict-backed store. Saves and loads deep copies (persistence semantics)."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}

    def save(self, record: RunRecord) -> None:
        self._records[record.id] = record.model_copy(deep=True)

    def load(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record is None:
            raise RunNotFound(f"no run '{run_id}'", run_id=run_id)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        record = self._records.get(run_id)
        return None if record is None else record.model_copy(deep=True)

    def list_ids(self) -> list[str]:
        return sorted(self._records)

    def delete(self, run_id: str) -> bool:
        return self._records.pop(run_id, None) is not None


class FileRunStore:
    """One JSON file per run under ``directory``; atomic writes.

    A crash between the temp write and the ``os.replace`` leaves either the
    old record or the new one - never a torn file. ``directory`` is created
    on first save.
    """

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory).expanduser()

    @property
    def directory(self) -> Path:
        """Where run files live."""
        return self._directory

    def _path(self, run_id: str) -> Path:
        # Run ids are ULIDs; reject anything that could escape the directory.
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise RunNotFound(f"no run '{run_id}'", run_id=run_id)
        return self._directory / f"{run_id}.json"

    def save(self, record: RunRecord) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            target = self._path(record.id)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(record.model_dump_json(by_alias=True), encoding="utf-8")
            tmp.replace(target)  # atomic on POSIX and Windows (os.replace semantics)
        except OSError as exc:
            raise RunStoreError(
                f"failed to persist run {record.id}: {exc}", run_id=record.id
            ) from exc

    def load(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record is None:
            raise RunNotFound(f"no run '{run_id}'", run_id=run_id)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"run file {path} is unreadable: {exc}", run_id=run_id) from exc

    def list_ids(self) -> list[str]:
        if not self._directory.is_dir():
            return []
        return sorted(path.stem for path in self._directory.glob("*.json"))

    def delete(self, run_id: str) -> bool:
        path = self._path(run_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
