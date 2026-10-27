"""Append-only ledger of :class:`~app.contracts.ledger.ChangeEntry` records.

ADR-0002 lives here:

* entries are **append-only** and **never mutated** after they enter the ledger;
* every read returns a defensive copy so caller-side mutation of ``targets``,
  ``issues_opened`` or ``issues_closed`` cannot corrupt history;
* token totals, completed task ids, consecutive failure streaks and issue churn
  are derived deterministically from the recorded entries;
* optional JSON Lines (``.jsonl``) persistence lets checkpoints snapshot and
  reload the full ledger without losing provenance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from app.contracts.common import Contract, IssueId
from app.contracts.enums import AgentRole, ChangeStatus
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.memory.errors import DuplicateEntryError, EntryNotFoundError, LedgerError

__all__ = [
    "ChangeLedger",
    "InMemoryLedger",
    "IssueSummary",
    "LedgerStore",
    "sum_token_usage",
]


def sum_token_usage(usages: Iterable[TokenUsage]) -> TokenUsage:
    """Sum an iterable of :class:`TokenUsage` records without double-counting."""
    total_in = 0
    total_out = 0
    total_cached = 0
    for usage in usages:
        total_in += usage.in_
        total_out += usage.out
        total_cached += usage.cached
    return TokenUsage(**{"in": total_in, "out": total_out, "cached": total_cached})


class IssueSummary(Contract):
    """Issue opening, closing and churn metrics derived from the ledger.

    Used by the digest builder to list active defects and by the loop controller to
    check the ``issue_churn`` policy threshold.
    """

    open_issues: list[IssueId] = Field(default_factory=list)
    closed_issues: list[IssueId] = Field(default_factory=list)
    total_opened: int = Field(default=0, ge=0)
    total_closed: int = Field(default=0, ge=0)

    @property
    def churn_ratio(self) -> float:
        """Ratio of still-open issues to total opened issues, in ``0.0..1.0``.

        When no issues have been opened yet, churn is ``0.0``. When issues are
        opened and never closed, the ratio approaches ``1.0``.
        """
        if self.total_opened == 0:
            return 0.0
        still_open = len(self.open_issues)
        return min(1.0, max(0.0, still_open / self.total_opened))


@runtime_checkable
class LedgerStore(Protocol):
    """Protocol for the append-only run ledger."""

    def append(self, entry: ChangeEntry | Mapping[str, Any]) -> ChangeEntry:
        """Append one entry to the ledger and return a defensive copy."""
        ...

    def get(self, entry_id: str) -> ChangeEntry | None:
        """Return the entry with ``entry_id``, or ``None``."""
        ...

    def require(self, entry_id: str) -> ChangeEntry:
        """Return the entry with ``entry_id`` or raise :class:`EntryNotFoundError`."""
        ...

    def entries(self) -> list[ChangeEntry]:
        """All entries in chronological append order."""
        ...

    def window(self, recent_window: int) -> tuple[list[ChangeEntry], list[ChangeEntry]]:
        """Split the ledger into ``(older_entries, recent_entries)``."""
        ...

    def total_tokens(
        self,
        *,
        iteration: int | None = None,
        role: AgentRole | str | None = None,
    ) -> TokenUsage:
        """Summed token usage across matching entries."""
        ...

    def issue_summary(self) -> IssueSummary:
        """Derived issue state across all appended entries."""
        ...

    def completed_task_ids(self) -> set[str]:
        """Task ids that have at least one ``status=ok`` ledger entry."""
        ...


class InMemoryLedger:
    """Append-only in-memory ledger with optional JSONL checkpoint backing.

    Args:
        entries: optional initial entries (validated and copied in order).
        path: optional ``.jsonl`` file path; when set, each appended entry is
            flushed as a single JSON line so a crash leaves an auditable trail.
        enforce_monotonic_iter: when ``True`` (default), appending an entry whose
            ``iter`` is smaller than an already-recorded iteration raises
            :class:`LedgerError`.
    """

    def __init__(
        self,
        entries: Iterable[ChangeEntry | Mapping[str, Any]] | None = None,
        *,
        path: Path | None = None,
        enforce_monotonic_iter: bool = True,
    ) -> None:
        self._entries: list[ChangeEntry] = []
        self._by_id: dict[str, int] = {}
        self._max_iter: int = -1
        self._path = path
        self._enforce_monotonic_iter = enforce_monotonic_iter

        if entries is not None:
            for item in entries:
                self.append(item)

    # -- size & iteration --------------------------------------------------
    def __len__(self) -> int:
        """Number of entries currently in the ledger."""
        return len(self._entries)

    @property
    def count(self) -> int:
        """Number of entries in the ledger."""
        return len(self._entries)

    @property
    def latest_iteration(self) -> int:
        """Highest iteration index recorded so far, or ``0`` when empty."""
        return max(0, self._max_iter)

    @property
    def path(self) -> Path | None:
        """Optional JSONL backing file path."""
        return self._path

    # -- write (append-only) -----------------------------------------------
    def append(self, entry: ChangeEntry | Mapping[str, Any]) -> ChangeEntry:
        """Validate, deep-copy and append one :class:`ChangeEntry`.

        Raises:
            DuplicateEntryError: an entry with the same ``id`` already exists.
            LedgerError: ``entry.iter`` moves backwards when monotonic iteration
                enforcement is enabled, or the payload is invalid.
        """
        validated = self._coerce_entry(entry)
        if validated.id in self._by_id:
            raise DuplicateEntryError(
                f"ledger is append-only; entry {validated.id!r} already exists",
                entry_id=validated.id,
            )
        if self._enforce_monotonic_iter and validated.iter < self._max_iter:
            raise LedgerError(
                f"entry iteration {validated.iter} precedes latest recorded iteration "
                f"{self._max_iter}",
                entry_id=validated.id,
                entry_iter=validated.iter,
                latest_iter=self._max_iter,
            )

        stored = validated.model_copy(deep=True)
        if self._path is not None:
            self._append_jsonl_line(self._path, stored)

        self._by_id[stored.id] = len(self._entries)
        self._entries.append(stored)
        if stored.iter > self._max_iter:
            self._max_iter = stored.iter

        return stored.model_copy(deep=True)

    def extend(self, entries: Iterable[ChangeEntry | Mapping[str, Any]]) -> list[ChangeEntry]:
        """Append several entries in order, returning their defensive copies."""
        return [self.append(entry) for entry in entries]

    # -- reads (defensive copies only) -------------------------------------
    def get(self, entry_id: str) -> ChangeEntry | None:
        """Look up an entry by ULID, or return ``None``."""
        index = self._by_id.get(entry_id)
        if index is None:
            return None
        return self._entries[index].model_copy(deep=True)

    def require(self, entry_id: str) -> ChangeEntry:
        """Look up an entry by ULID, or raise :class:`EntryNotFoundError`."""
        entry = self.get(entry_id)
        if entry is None:
            raise EntryNotFoundError(
                f"no ledger entry with id {entry_id!r}",
                entry_id=entry_id,
            )
        return entry

    def entries(self) -> list[ChangeEntry]:
        """Every entry in append order, deep-copied so callers cannot mutate history."""
        return [entry.model_copy(deep=True) for entry in self._entries]

    def by_iteration(self, iteration: int) -> list[ChangeEntry]:
        """Entries recorded in ``iteration``, in append order."""
        return [entry.model_copy(deep=True) for entry in self._entries if entry.iter == iteration]

    for_iteration = by_iteration

    def by_role(self, role: AgentRole | str) -> list[ChangeEntry]:
        """Entries authored by ``role``, in append order (``'orchestrator'`` works too)."""
        role_value = role.value if isinstance(role, AgentRole) else str(role)
        return [
            entry.model_copy(deep=True) for entry in self._entries if entry.role_name == role_value
        ]

    for_role = by_role

    def by_task(self, task_id: str) -> list[ChangeEntry]:
        """Entries belonging to ``task_id``, in append order."""
        return [entry.model_copy(deep=True) for entry in self._entries if entry.task_id == task_id]

    for_task = by_task

    def by_target(self, target: str) -> list[ChangeEntry]:
        """Entries touching ``target`` (a section slug or entity ULID)."""
        return [entry.model_copy(deep=True) for entry in self._entries if target in entry.targets]

    for_target = by_target

    def by_status(self, status: ChangeStatus) -> list[ChangeEntry]:
        """Entries carrying ``status``, in append order."""
        return [entry.model_copy(deep=True) for entry in self._entries if entry.status is status]

    # -- windowing for digest & compactor ----------------------------------
    def recent(self, recent_window: int) -> list[ChangeEntry]:
        """The last ``recent_window`` entries in chronological order."""
        if recent_window <= 0:
            return []
        return [entry.model_copy(deep=True) for entry in self._entries[-recent_window:]]

    def older_than(self, recent_window: int) -> list[ChangeEntry]:
        """Entries preceding the last ``recent_window`` entries."""
        if recent_window <= 0:
            return self.entries()
        cutoff = max(0, len(self._entries) - recent_window)
        return [entry.model_copy(deep=True) for entry in self._entries[:cutoff]]

    def window(self, recent_window: int) -> tuple[list[ChangeEntry], list[ChangeEntry]]:
        """Split the ledger into ``(older_entries, recent_entries)``.

        Raises:
            LedgerError: ``recent_window`` is negative.
        """
        if recent_window < 0:
            raise LedgerError(
                f"recent_window must be >= 0, got {recent_window}",
                recent_window=recent_window,
            )
        return self.older_than(recent_window), self.recent(recent_window)

    # -- derived metrics ---------------------------------------------------
    def completed_task_ids(self) -> set[str]:
        """Task ids that have at least one ``status=ok`` *agent* entry.

        Orchestrator entries are excluded: a rollback or emergency action can be
        recorded as ``ok`` while touching a task's ``task_id``, and a task the loop
        merely reported on has not been executed.
        """
        return {
            entry.task_id
            for entry in self._entries
            if entry.status is ChangeStatus.OK and not entry.is_orchestrator
        }

    def needs_user_input_entries(self) -> list[ChangeEntry]:
        """Entries that halted with ``status=needs_user_input`` (HARD RULE 1)."""
        return self.by_status(ChangeStatus.NEEDS_USER_INPUT)

    def failed_streak(self) -> int:
        """Number of consecutive ``status=failed`` entries at the tail of the ledger."""
        streak = 0
        for entry in reversed(self._entries):
            if entry.status is ChangeStatus.FAILED:
                streak += 1
            else:
                break
        return streak

    def total_tokens(
        self,
        *,
        iteration: int | None = None,
        role: AgentRole | str | None = None,
    ) -> TokenUsage:
        """Summed :class:`TokenUsage` across entries matching the optional filters."""
        role_value = (
            role.value if isinstance(role, AgentRole) else (str(role) if role is not None else None)
        )
        matching = (
            entry.tokens
            for entry in self._entries
            if (iteration is None or entry.iter == iteration)
            and (role_value is None or entry.role_name == role_value)
        )
        return sum_token_usage(matching)

    def iteration_tokens(self, iteration: int) -> TokenUsage:
        """Summed :class:`TokenUsage` for ``iteration``."""
        return self.total_tokens(iteration=iteration)

    def open_issues(self) -> list[str]:
        """Currently open issue ids in first-opened order."""
        return self.issue_summary().open_issues

    def closed_issues(self) -> list[str]:
        """Closed issue ids that are not currently open."""
        return self.issue_summary().closed_issues

    def issue_summary(self) -> IssueSummary:
        """Compute the current open/closed issue sets and churn ratio."""
        active: list[str] = []
        closed: list[str] = []
        total_opened = 0
        total_closed = 0

        for entry in self._entries:
            for issue_id in entry.issues_opened:
                total_opened += 1
                if issue_id in closed:
                    closed.remove(issue_id)
                if issue_id not in active:
                    active.append(issue_id)
            for issue_id in entry.issues_closed:
                total_closed += 1
                if issue_id in active:
                    active.remove(issue_id)
                if issue_id not in closed:
                    closed.append(issue_id)

        return IssueSummary(
            open_issues=active,
            closed_issues=closed,
            total_opened=total_opened,
            total_closed=total_closed,
        )

    # -- JSONL persistence -------------------------------------------------
    def save_jsonl(self, path: Path) -> None:
        """Write the entire ledger as UTF-8 JSON Lines (one ChangeEntry per line)."""
        lines = [entry.model_dump_json(by_alias=True) for entry in self._entries]
        payload = ("\n".join(lines) + "\n") if lines else ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            raise LedgerError(
                f"failed to save ledger JSONL to {path}: {exc}",
                path=str(path),
            ) from exc

    @classmethod
    def from_jsonl(
        cls,
        path: Path,
        *,
        attach_path: bool = False,
        enforce_monotonic_iter: bool = True,
    ) -> InMemoryLedger:
        """Load a ledger from a JSON Lines file.

        Raises:
            LedgerError: the file does not exist, cannot be read, or contains a
                malformed line.
        """
        if not path.exists():
            raise LedgerError(f"ledger JSONL file not found: {path}", path=str(path))

        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LedgerError(
                f"failed to read ledger JSONL file {path}: {exc}",
                path=str(path),
            ) from exc

        loaded: list[ChangeEntry] = []
        for line_number, line in enumerate(raw_lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded.append(ChangeEntry.model_validate_json(stripped))
            except Exception as exc:
                raise LedgerError(
                    f"invalid ChangeEntry at {path}:{line_number}: {exc}",
                    path=str(path),
                    line=line_number,
                ) from exc

        return cls(
            loaded,
            path=path if attach_path else None,
            enforce_monotonic_iter=enforce_monotonic_iter,
        )

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _coerce_entry(entry: ChangeEntry | Mapping[str, Any]) -> ChangeEntry:
        if isinstance(entry, ChangeEntry):
            return entry
        if isinstance(entry, Mapping):
            try:
                return ChangeEntry.model_validate(dict(entry))
            except Exception as exc:
                raise LedgerError(f"invalid ChangeEntry payload: {exc}") from exc
        raise LedgerError(f"expected ChangeEntry or mapping, got {type(entry).__name__}")

    @staticmethod
    def _append_jsonl_line(path: Path, entry: ChangeEntry) -> None:
        line = entry.model_dump_json(by_alias=True) + "\n"
        initial_size: int | None = None
        try:
            if path.exists() and path.is_file():
                initial_size = path.stat().st_size
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except OSError as exc:
            if initial_size is not None:
                try:
                    with path.open("r+b") as cleanup_handle:
                        cleanup_handle.truncate(initial_size)
                except OSError:
                    pass
            raise LedgerError(
                f"failed to persist ChangeEntry {entry.id!r} to {path}: {exc}",
                entry_id=entry.id,
                path=str(path),
            ) from exc


#: Alias used by callers that name the concrete store after its contract.
ChangeLedger = InMemoryLedger
