"""The article store: independently versioned sections, snapshots, best-so-far.

Each section is its own versioned document. A patch bumps one section's version and
nothing else; the store never rewrites history, so ``restore`` appends a new version
rather than editing an old one. That is what makes rolling back from erosion cheap
and auditable: the ledger, the version list and the snapshot all still agree.

Concurrency is optimistic plus a per-section lock:

* :class:`~app.article.patch.StalePatch` when ``base_version`` no longer matches the
  head - the patch is discarded, never merged;
* :class:`~app.article.locks.SectionLocks` around every read-modify-write, so two
  agents editing *different* sections never wait for each other and two agents
  editing the *same* section serialise.

Storage is behind :class:`SectionStorage`. :class:`InMemoryStorage` is the default
(and what tests use); the SQLAlchemy backend is a drop-in implementation of the same
eight methods, which keeps this module free of ORM imports and of Alembic coupling.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from pydantic import ValidationError

from app.article.assemble import (
    AssembledArticle,
    assemble,
    export_markdown,
    section_from,
)
from app.article.locks import DEFAULT_LOCK_TIMEOUT, SectionLocks
from app.article.patch import (
    DiffStats,
    PatchOutcome,
    StalePatch,
    TextDiff,
    apply_ops,
    diff_texts,
)

# Aliased: the store exposes methods with these names, so the module-level names
# would otherwise be shadowed at exactly the point where they are called.
from app.article.recurrence import edits_without_gain as _edits_without_gain
from app.article.recurrence import hash_recurred as _hash_recurred
from app.contracts.common import word_count
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import Patch, PatchOp
from app.contracts.section import EMPTY_SECTION_HASH, CitationRef, SectionState
from app.core.clock import utc_now
from app.core.errors import ContractViolation, NotFoundError, format_validation_errors
from app.core.hashing import content_hash
from app.core.ids import new_ulid

__all__ = [
    "MAX_CITATIONS",
    "AppliedPatch",
    "ArticleStore",
    "DiffResult",
    "InMemoryStorage",
    "RestoreResult",
    "SectionBlueprint",
    "SectionStorage",
    "Snapshot",
    "SnapshotSection",
    "StoredSection",
    "StoredVersion",
    "VersionInfo",
    "extract_citation_keys",
]

#: Contract ceiling on ``SectionState.citations``; extraction is truncated to it.
MAX_CITATIONS: Final[int] = 500
#: Citation markers are truncated to ``CitationRef.key``'s limit.
MAX_CITATION_KEY_CHARS: Final[int] = 120

#: Author-year parentheticals, e.g. ``(Smith, 2020)`` / ``(Nguyen et al., 2024b)``.
_AUTHOR_YEAR_RE = re.compile(
    r"\([^()]{0,80}?\b(?:1[5-9]\d{2}|20\d{2}|21\d{2})[a-z]?\b[^()]{0,30}?\)"
)
#: Numeric markers, e.g. ``[1]``, ``[2, 3]``, ``[4-6]``.
_NUMERIC_RE = re.compile(r"\[\d{1,3}(?:\s*[,–-]\s*\d{1,3})*\]")


def extract_citation_keys(text: str) -> list[str]:
    """Citation markers found in ``text``, in first-occurrence order, de-duplicated.

    Deliberately permissive: a marker with no ``SourceCard`` behind it is exactly
    what the ``citations_resolve_both_ways`` gate reports, so over-matching is safer
    than silently dropping a citation site.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for pattern in (_AUTHOR_YEAR_RE, _NUMERIC_RE):
        for match in pattern.finditer(text):
            key = match.group(0)[:MAX_CITATION_KEY_CHARS]
            if key not in seen:
                seen.add(key)
                keys.append(key)
    keys.sort(key=lambda key: text.index(key))
    return keys


@dataclass(frozen=True)
class StoredSection:
    """One row: the head of a section's version list, plus its body."""

    id: str
    title: str
    order: int
    version: int
    hash: str
    words: int
    citations: tuple[CitationRef, ...]
    status: SectionStatus
    open_issues: tuple[str, ...]
    text: str
    updated_at: datetime

    def state(self) -> SectionState:
        """The contract view the Planner, the validator and the API all consume.

        Raises:
            ContractViolation: the row would break a ``SectionState`` invariant (an
                empty section carrying citations, a bad issue id, ...). Every write
                path calls this before persisting, so a bad row never reaches storage.
        """
        try:
            return SectionState(
                id=self.id,
                title=self.title,
                order=self.order,
                version=self.version,
                hash=self.hash,
                words=self.words,
                citations=list(self.citations),
                status=self.status,
                open_issues=list(self.open_issues),
            )
        except ValidationError as exc:
            raise ContractViolation(
                f"section '{self.id}' would violate SectionState: {format_validation_errors(exc)}",
                section_id=self.id,
            ) from exc


@dataclass(frozen=True)
class StoredVersion:
    """One immutable entry in a section's history. Never edited, only appended."""

    section_id: str
    version: int
    hash: str
    words: int
    text: str
    status: SectionStatus
    patch_id: str | None
    entry_id: str | None
    chars_added: int
    chars_removed: int
    created_at: datetime
    #: Q measured while this version was the head; ``None`` until somebody measures.
    q_after: float | None = None

    def info(self) -> VersionInfo:
        """The text-free view: history travels by reference (HARD RULE 3)."""
        return VersionInfo(
            section_id=self.section_id,
            version=self.version,
            hash=self.hash,
            words=self.words,
            status=self.status,
            chars_added=self.chars_added,
            chars_removed=self.chars_removed,
            patch_id=self.patch_id,
            created_at=self.created_at,
            q_after=self.q_after,
        )


@dataclass(frozen=True)
class VersionInfo:
    """What :meth:`ArticleStore.history` returns: metadata and hashes, no bodies."""

    section_id: str
    version: int
    hash: str
    words: int
    status: SectionStatus
    chars_added: int
    chars_removed: int
    patch_id: str | None
    created_at: datetime
    q_after: float | None


@dataclass(frozen=True)
class SnapshotSection:
    """One section frozen inside a snapshot: state plus the exact body."""

    state: SectionState
    text: str


@dataclass(frozen=True)
class Snapshot:
    """A whole-article checkpoint. ``q`` is what makes one "best so far"."""

    id: str
    label: str
    q: float | None
    iter: int | None
    created_at: datetime
    sections: tuple[SnapshotSection, ...]

    @property
    def section_ids(self) -> tuple[str, ...]:
        """Ids in snapshot order."""
        return tuple(section.state.id for section in self.sections)

    @property
    def total_words(self) -> int:
        """Body words across the snapshot."""
        return sum(section.state.words for section in self.sections)

    def text_of(self, section_id: str) -> str | None:
        """The frozen body of one section, or ``None`` when it is not in the snapshot."""
        for section in self.sections:
            if section.state.id == section_id:
                return section.text
        return None

    def state_of(self, section_id: str) -> SectionState | None:
        """The frozen state of one section, or ``None``."""
        for section in self.sections:
            if section.state.id == section_id:
                return section.state
        return None


class SectionStorage(Protocol):
    """What a persistence backend must provide.

    Implementations do not need to be transactional across sections: every store
    write happens inside a per-section lock, and snapshots are written as one
    immutable row.
    """

    def read_section(self, section_id: str) -> StoredSection | None:
        """The section head row, or ``None``."""
        ...

    def read_sections(self) -> list[StoredSection]:
        """Every section head row, in any order."""
        ...

    def write_section(self, section: StoredSection) -> None:
        """Insert or replace a section head row."""
        ...

    def delete_section(self, section_id: str) -> None:
        """Drop a section and its history. No-op when it does not exist."""
        ...

    def append_version(self, version: StoredVersion) -> None:
        """Append one immutable history row."""
        ...

    def read_versions(self, section_id: str) -> list[StoredVersion]:
        """All history rows for a section, in any order."""
        ...

    def update_version(self, version: StoredVersion) -> None:
        """Replace the row with the same ``(section_id, version)``.

        Used for exactly one thing: stamping ``q_after`` once a Q has been measured.
        """
        ...

    def write_snapshot(self, snapshot: Snapshot) -> None:
        """Store an immutable whole-article checkpoint."""
        ...

    def read_snapshot(self, snapshot_id: str) -> Snapshot | None:
        """One snapshot by id."""
        ...

    def read_snapshots(self) -> list[Snapshot]:
        """Every snapshot, in any order."""
        ...


class InMemoryStorage:
    """Dict-backed :class:`SectionStorage`. The default, and what tests run on."""

    def __init__(self) -> None:
        self._sections: dict[str, StoredSection] = {}
        self._versions: dict[str, list[StoredVersion]] = {}
        self._snapshots: dict[str, Snapshot] = {}

    def read_section(self, section_id: str) -> StoredSection | None:
        """See :meth:`SectionStorage.read_section`."""
        return self._sections.get(section_id)

    def read_sections(self) -> list[StoredSection]:
        """See :meth:`SectionStorage.read_sections`."""
        return list(self._sections.values())

    def write_section(self, section: StoredSection) -> None:
        """See :meth:`SectionStorage.write_section`."""
        self._sections[section.id] = section

    def delete_section(self, section_id: str) -> None:
        """See :meth:`SectionStorage.delete_section`."""
        self._sections.pop(section_id, None)
        self._versions.pop(section_id, None)

    def append_version(self, version: StoredVersion) -> None:
        """See :meth:`SectionStorage.append_version`."""
        self._versions.setdefault(version.section_id, []).append(version)

    def read_versions(self, section_id: str) -> list[StoredVersion]:
        """See :meth:`SectionStorage.read_versions`."""
        return list(self._versions.get(section_id, ()))

    def update_version(self, version: StoredVersion) -> None:
        """See :meth:`SectionStorage.update_version`."""
        rows = self._versions.get(version.section_id, [])
        for index, row in enumerate(rows):
            if row.version == version.version:
                rows[index] = version
                return
        raise NotFoundError(
            f"no version {version.version} for section '{version.section_id}'",
            section_id=version.section_id,
            version=version.version,
        )

    def write_snapshot(self, snapshot: Snapshot) -> None:
        """See :meth:`SectionStorage.write_snapshot`."""
        self._snapshots[snapshot.id] = snapshot

    def read_snapshot(self, snapshot_id: str) -> Snapshot | None:
        """See :meth:`SectionStorage.read_snapshot`."""
        return self._snapshots.get(snapshot_id)

    def read_snapshots(self) -> list[Snapshot]:
        """See :meth:`SectionStorage.read_snapshots`."""
        return list(self._snapshots.values())


@runtime_checkable
class SectionBlueprint(Protocol):
    """Anything that names a section to create - ``SectionSpec`` satisfies this."""

    id: str
    title: str


@dataclass(frozen=True)
class AppliedPatch:
    """What the store reports after applying a patch."""

    section_id: str
    patch_id: str
    base_version: int
    version: int
    hash: str
    words: int
    status: SectionStatus
    stats: DiffStats
    ops_applied: int
    changed: bool
    created_at: datetime


@dataclass(frozen=True)
class DiffResult:
    """Diff between two versions of one section."""

    section_id: str
    from_version: int
    to_version: int
    from_hash: str
    to_hash: str
    diff: TextDiff


@dataclass(frozen=True)
class RestoreResult:
    """What a rollback actually changed."""

    snapshot_id: str
    label: str
    q: float | None
    restored: tuple[str, ...]
    unchanged: tuple[str, ...]
    versions: Mapping[str, int]


class ArticleStore:
    """Versioned sections, patches, snapshots and the best-so-far pointer."""

    def __init__(
        self,
        storage: SectionStorage | None = None,
        *,
        locks: SectionLocks | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = new_ulid,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._storage: SectionStorage = storage if storage is not None else InMemoryStorage()
        self._locks = locks if locks is not None else SectionLocks(timeout=lock_timeout)
        self._clock = clock
        self._new_id = id_factory

    # --- plumbing ---------------------------------------------------------

    @property
    def locks(self) -> SectionLocks:
        """The lock manager, exposed so the orchestrator can widen a critical section."""
        return self._locks

    @property
    def storage(self) -> SectionStorage:
        """The persistence backend."""
        return self._storage

    def section_ids(self) -> list[str]:
        """Every section id, in document order. Synchronous: no await, no lock."""
        return [row.id for row in self._rows()]

    def _rows(self) -> list[StoredSection]:
        return sorted(self._storage.read_sections(), key=lambda row: (row.order, row.id))

    def _require(self, section_id: str) -> StoredSection:
        row = self._storage.read_section(section_id)
        if row is None:
            raise NotFoundError(f"no section '{section_id}'", section_id=section_id)
        return row

    def _next_order(self) -> int:
        rows = self._storage.read_sections()
        return 0 if not rows else max(row.order for row in rows) + 1

    # --- sections ---------------------------------------------------------

    async def create_section(
        self, section_id: str, title: str, *, order: int | None = None, text: str | None = None
    ) -> StoredSection:
        """Create an empty section; with ``text``, also write its first version.

        Raises:
            ContractViolation: the id already exists, or the id/title/order fails the
                ``SectionState`` contract.
        """
        async with self._locks.hold(section_id):
            if self._storage.read_section(section_id) is not None:
                raise ContractViolation(
                    f"section '{section_id}' already exists", section_id=section_id
                )
            row = StoredSection(
                id=section_id,
                title=title,
                order=self._next_order() if order is None else order,
                version=0,
                hash=EMPTY_SECTION_HASH,
                words=0,
                citations=(),
                status=SectionStatus.EMPTY,
                open_issues=(),
                text="",
                updated_at=self._clock(),
            )
            row.state()  # validate against the contract before anything is stored
            self._storage.write_section(row)

        if text is None:
            return row
        # Outside the lock: the first content write goes through the normal patch
        # path, and locks are deliberately not reentrant.
        await self.patch_section(section_id, [PatchOp(op=PatchOpType.REPLACE_SECTION, text=text)])
        return await self.get_section(section_id)

    async def create_sections(self, blueprints: Sequence[SectionBlueprint]) -> list[StoredSection]:
        """Create the rubric's sections in list order. Order comes from position."""
        created: list[StoredSection] = []
        for order, blueprint in enumerate(blueprints):
            created.append(await self.create_section(blueprint.id, blueprint.title, order=order))
        return created

    async def get_section(self, section_id: str) -> StoredSection:
        """The head row of one section.

        Raises:
            NotFoundError: no such section.
        """
        async with self._locks.hold(section_id):
            return self._require(section_id)

    async def get_text(self, section_id: str) -> str:
        """The current body of one section."""
        section = await self.get_section(section_id)
        return section.text

    async def list_state(self) -> list[SectionState]:
        """Every section's contract state, in document order. Never the bodies."""
        async with self._locks.hold_many(self.section_ids()):
            return [row.state() for row in self._rows()]

    async def delete_section(self, section_id: str) -> None:
        """Remove a section and its history."""
        async with self._locks.hold(section_id):
            self._require(section_id)
            self._storage.delete_section(section_id)

    async def rename_section(self, section_id: str, title: str) -> StoredSection:
        """Change a section's title. Content, version and status are untouched."""
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            updated = replace(row, title=title, updated_at=self._clock())
            updated.state()
            self._storage.write_section(updated)
            return updated

    async def reorder(self, ordered_ids: Sequence[str]) -> list[StoredSection]:
        """Set ``order`` from the given sequence. Ids must cover every section."""
        known = self.section_ids()
        missing = sorted(set(known) - set(ordered_ids))
        unknown = sorted(set(ordered_ids) - set(known))
        if missing or unknown:
            raise ContractViolation(
                "reorder needs exactly the current section ids",
                missing=missing,
                unknown=unknown,
            )
        async with self._locks.hold_many(ordered_ids):
            for order, section_id in enumerate(ordered_ids):
                row = self._require(section_id)
                self._storage.write_section(replace(row, order=order, updated_at=self._clock()))
            return self._rows()

    async def set_status(self, section_id: str, status: SectionStatus) -> StoredSection:
        """Promote or demote a section.

        ``empty`` is never a valid target - a section that has had text keeps its
        history and its version number, so it can only be ``draft`` at worst.
        """
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            if status is SectionStatus.EMPTY:
                raise ContractViolation(
                    f"section '{section_id}' cannot be set back to 'empty'; "
                    "delete its text with a patch instead",
                    section_id=section_id,
                )
            if row.version == 0:
                raise ContractViolation(
                    f"section '{section_id}' has no content, so it cannot be '{status.value}'",
                    section_id=section_id,
                )
            updated = replace(row, status=status, updated_at=self._clock())
            updated.state()
            self._storage.write_section(updated)
            return updated

    async def set_open_issues(self, section_id: str, issues: Sequence[str]) -> StoredSection:
        """Replace the section's open issue list (ids are validated by the contract)."""
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            updated = replace(
                row, open_issues=tuple(dict.fromkeys(issues)), updated_at=self._clock()
            )
            updated.state()
            self._storage.write_section(updated)
            return updated

    async def set_citations(
        self, section_id: str, citations: Sequence[CitationRef]
    ) -> StoredSection:
        """Replace the section's citation sites."""
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            updated = replace(row, citations=tuple(citations), updated_at=self._clock())
            updated.state()
            self._storage.write_section(updated)
            return updated

    async def resolve_citation(
        self, section_id: str, key: str, source_id: str | None
    ) -> StoredSection:
        """Attach (or detach) a ``SourceCard`` to one citation marker."""
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            merged: list[CitationRef] = []
            found = False
            for citation in row.citations:
                if citation.key == key:
                    found = True
                    merged.append(CitationRef(key=key, source_id=source_id))
                else:
                    merged.append(citation)
            if not found:
                raise NotFoundError(
                    f"section '{section_id}' has no citation '{key}'",
                    section_id=section_id,
                    key=key,
                )
            updated = replace(row, citations=tuple(merged), updated_at=self._clock())
            self._storage.write_section(updated)
            return updated

    # --- patching ---------------------------------------------------------

    async def apply_patch(self, patch: Patch, *, entry_id: str | None = None) -> AppliedPatch:
        """Apply an agent's patch under the section lock.

        Raises:
            NotFoundError: the section does not exist.
            StalePatch: ``base_version`` is not the head version.
            PatchRejected: an op does not match; see
                :class:`~app.article.patch.PatchRejected` for the repair instruction.
        """
        async with self._locks.hold(patch.section_id):
            row = self._require(patch.section_id)
            if patch.base_version != row.version:
                raise StalePatch(
                    section_id=patch.section_id,
                    base_version=patch.base_version,
                    current_version=row.version,
                )
            return self._commit(row, patch, entry_id=entry_id)

    async def patch_section(
        self,
        section_id: str,
        ops: Sequence[PatchOp],
        *,
        patch_id: str | None = None,
        entry_id: str | None = None,
    ) -> AppliedPatch:
        """Read-modify-write: build the patch against the head, so it cannot be stale.

        This is the path the orchestrator uses for its own writes and the one tests
        use to drive contention; agents that author patches from a digest use
        :meth:`apply_patch` and earn their ``StalePatch``.
        """
        async with self._locks.hold(section_id):
            row = self._require(section_id)
            patch = Patch(
                section_id=section_id,
                base_version=row.version,
                patch_id=patch_id,
                ops=list(ops),
            )
            return self._commit(row, patch, entry_id=entry_id)

    async def apply_patches(
        self, patches: Sequence[Patch], *, entry_id: str | None = None
    ) -> list[AppliedPatch]:
        """Apply patches touching several sections in one deadlock-safe critical section.

        Locks are taken in :func:`~app.article.locks.order_section_ids` order. The
        batch is all-or-nothing at the *validation* level only: a patch that raises
        stops the batch, and the sections already written keep their new versions
        (each of them is individually consistent and individually revertible).
        """
        section_ids = [patch.section_id for patch in patches]
        async with self._locks.hold_many(section_ids):
            rows = {section_id: self._require(section_id) for section_id in section_ids}
            applied: list[AppliedPatch] = []
            for patch in patches:
                row = rows[patch.section_id]
                if patch.base_version != row.version:
                    raise StalePatch(
                        section_id=patch.section_id,
                        base_version=patch.base_version,
                        current_version=row.version,
                    )
                result = self._commit(row, patch, entry_id=entry_id)
                rows[patch.section_id] = self._require(patch.section_id)
                applied.append(result)
            return applied

    def _commit(self, row: StoredSection, patch: Patch, *, entry_id: str | None) -> AppliedPatch:
        """Validate, apply and persist. Caller holds the section lock."""
        outcome: PatchOutcome = apply_ops(row.text, patch, status=row.status)
        patch_id = patch.patch_id or self._new_id()
        created_at = self._clock()

        if not outcome.changed:
            # Whitespace-only edits are not progress: no version, no history row.
            return AppliedPatch(
                section_id=row.id,
                patch_id=patch_id,
                base_version=row.version,
                version=row.version,
                hash=row.hash,
                words=row.words,
                status=row.status,
                stats=outcome.stats,
                ops_applied=outcome.ops_applied,
                changed=False,
                created_at=created_at,
            )

        new_version = row.version + 1
        state = row.state().advanced(version=new_version, hash_=outcome.hash, words=outcome.words)
        updated = replace(
            row,
            version=new_version,
            hash=outcome.hash,
            words=outcome.words,
            status=state.status,
            citations=_merged_citations(row.citations, outcome.text),
            text=outcome.text,
            updated_at=created_at,
        )
        self._storage.write_section(updated)
        self._storage.append_version(
            StoredVersion(
                section_id=row.id,
                version=new_version,
                hash=outcome.hash,
                words=outcome.words,
                text=outcome.text,
                status=state.status,
                patch_id=patch_id,
                entry_id=entry_id,
                chars_added=outcome.stats.chars_added,
                chars_removed=outcome.stats.chars_removed,
                created_at=created_at,
            )
        )
        return AppliedPatch(
            section_id=row.id,
            patch_id=patch_id,
            base_version=row.version,
            version=new_version,
            hash=outcome.hash,
            words=outcome.words,
            status=state.status,
            stats=outcome.stats,
            ops_applied=outcome.ops_applied,
            changed=True,
            created_at=created_at,
        )

    # --- history ----------------------------------------------------------

    async def history(self, section_id: str) -> list[VersionInfo]:
        """Version list oldest-first: versions, hashes and diff sizes, never bodies."""
        async with self._locks.hold(section_id):
            self._require(section_id)
            rows = sorted(self._storage.read_versions(section_id), key=lambda row: row.version)
            return [row.info() for row in rows]

    async def version_text(self, section_id: str, version: int) -> str:
        """The exact body stored at ``version``."""
        async with self._locks.hold(section_id):
            row = self._version_row(section_id, version)
            return row.text

    async def diff(self, section_id: str, v_a: int, v_b: int) -> DiffResult:
        """Unified diff and change sizes between two versions of one section."""
        async with self._locks.hold(section_id):
            left = self._version_row(section_id, v_a)
            right = self._version_row(section_id, v_b)
            return DiffResult(
                section_id=section_id,
                from_version=left.version,
                to_version=right.version,
                from_hash=left.hash,
                to_hash=right.hash,
                diff=diff_texts(
                    left.text,
                    right.text,
                    from_label=f"v{left.version}",
                    to_label=f"v{right.version}",
                ),
            )

    def _version_row(self, section_id: str, version: int) -> StoredVersion:
        for row in self._storage.read_versions(section_id):
            if row.version == version:
                return row
        raise NotFoundError(
            f"section '{section_id}' has no version {version}",
            section_id=section_id,
            version=version,
        )

    # --- snapshots --------------------------------------------------------

    async def snapshot(
        self, label: str, *, q: float | None = None, iter: int | None = None
    ) -> Snapshot:
        """Freeze every section (state plus body) as one immutable checkpoint."""
        async with self._locks.hold_many(self.section_ids()):
            sections = tuple(
                SnapshotSection(state=row.state(), text=row.text) for row in self._rows()
            )
            snapshot = Snapshot(
                id=self._new_id(),
                label=label,
                q=None if q is None else float(q),
                iter=iter,
                created_at=self._clock(),
                sections=sections,
            )
            self._storage.write_snapshot(snapshot)
            return snapshot

    async def snapshots(self) -> list[Snapshot]:
        """Every snapshot, newest first."""
        return sorted(
            self._storage.read_snapshots(),
            key=lambda snapshot: snapshot.created_at,
            reverse=True,
        )

    async def best_snapshot(self) -> Snapshot | None:
        """Highest-Q snapshot; ties break on the later iteration, then the later time.

        Snapshots taken without a Q are never "best": best-so-far is a statement
        about measured quality, not about recency. ``None`` when nothing is scored.
        """
        candidates = [
            snapshot for snapshot in self._storage.read_snapshots() if snapshot.q is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=_snapshot_rank)

    async def restore(self, snapshot_id: str) -> RestoreResult:
        """Roll the article back to a snapshot by appending new versions.

        History is never rewritten: each section whose body differs gets a new
        version carrying the snapshot's text, hash, word count and status. Sections
        that already match are left alone, and sections created after the snapshot
        are untouched - a rollback is not a delete.

        Raises:
            NotFoundError: no such snapshot.
        """
        snapshot = self._storage.read_snapshot(snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"no snapshot '{snapshot_id}'", snapshot_id=snapshot_id)

        targets = sorted(set(snapshot.section_ids) | set(self.section_ids()))
        restored: list[str] = []
        unchanged: list[str] = []
        versions: dict[str, int] = {}

        async with self._locks.hold_many(targets):
            for frozen_section in snapshot.sections:
                section_id = frozen_section.state.id
                text = frozen_section.text
                row = self._storage.read_section(section_id)
                if row is None:
                    row = self._recreate(frozen_section)
                if content_hash(row.text) == content_hash(text):
                    unchanged.append(section_id)
                    continue

                new_version = row.version + 1
                status = frozen_section.state.status
                if status is SectionStatus.EMPTY and new_version >= 1:
                    # A section with history cannot claim 'empty'; the body is gone
                    # but the version number is not.
                    status = SectionStatus.DRAFT
                created_at = self._clock()
                updated = replace(
                    row,
                    version=new_version,
                    hash=content_hash(text),
                    words=word_count(text),
                    status=status,
                    citations=_merged_citations(row.citations, text),
                    text=text,
                    updated_at=created_at,
                )
                self._storage.write_section(updated)
                self._storage.append_version(
                    StoredVersion(
                        section_id=section_id,
                        version=new_version,
                        hash=updated.hash,
                        words=updated.words,
                        text=text,
                        status=status,
                        patch_id=None,
                        entry_id=None,
                        chars_added=0,
                        chars_removed=0,
                        created_at=created_at,
                        q_after=snapshot.q,
                    )
                )
                restored.append(section_id)
                versions[section_id] = new_version

        return RestoreResult(
            snapshot_id=snapshot.id,
            label=snapshot.label,
            q=snapshot.q,
            restored=tuple(restored),
            unchanged=tuple(unchanged),
            versions=versions,
        )

    def _recreate(self, frozen_section: SnapshotSection) -> StoredSection:
        """Rebuild a section that was deleted after the snapshot was taken."""
        state = frozen_section.state
        row = StoredSection(
            id=state.id,
            title=state.title,
            order=state.order,
            version=0,
            hash=EMPTY_SECTION_HASH,
            words=0,
            citations=(),
            status=SectionStatus.EMPTY,
            open_issues=(),
            text="",
            updated_at=self._clock(),
        )
        self._storage.write_section(row)
        return row

    # --- quality ----------------------------------------------------------

    async def record_quality(self, q: float) -> int:
        """Stamp ``q`` on every section head that has no measurement yet.

        Q is measured per article, not per section, so one call after each validation
        is enough for the erosion detectors to see a per-section trajectory. Versions
        that already carry a measurement are left alone: Q belongs to the article as
        it was when it was measured, not to whatever the head happens to be now.
        Returns the number of versions stamped.
        """
        stamped = 0
        async with self._locks.hold_many(self.section_ids()):
            for row in self._rows():
                if row.version == 0:
                    continue
                head = self._head_row(row.id)
                if head is None or head.q_after is not None:
                    continue
                self._storage.update_version(replace(head, q_after=float(q)))
                stamped += 1
        return stamped

    def _head_row(self, section_id: str) -> StoredVersion | None:
        rows = self._storage.read_versions(section_id)
        if not rows:
            return None
        return max(rows, key=lambda row: row.version)

    async def hash_recurred(self, section_id: str, *, window: int) -> bool:
        """True when the section's head body repeats a body inside ``window``."""
        return _hash_recurred(await self.history(section_id), window=window)

    async def edits_without_gain(self, section_id: str, *, min_gain: float = 0.0) -> int:
        """Consecutive measured edits at the head that did not raise the best Q."""
        return _edits_without_gain(await self.history(section_id), min_gain=min_gain)

    # --- assembly ---------------------------------------------------------

    async def assembled(
        self,
        *,
        title: str | None = None,
        references: Sequence[str] = (),
        heading_level: int = 2,
        include_empty: bool = True,
    ) -> AssembledArticle:
        """Assemble the current head of every section into one document."""
        async with self._locks.hold_many(self.section_ids()):
            sections = [section_from(row.state(), row.text) for row in self._rows()]
        return assemble(
            sections,
            title=title,
            references=references,
            heading_level=heading_level,
            include_empty=include_empty,
        )

    async def to_markdown(self, *, title: str | None = None, references: Sequence[str] = ()) -> str:
        """The whole article as markdown."""
        document = await self.assembled(title=title, references=references)
        return document.markdown

    async def export_markdown(
        self, path: Path | str, *, title: str | None = None, references: Sequence[str] = ()
    ) -> Path:
        """Assemble and write ``.md`` to ``path``; returns the path written."""
        markdown = await self.to_markdown(title=title, references=references)
        return export_markdown(path, markdown)

    async def word_counts(self) -> dict[str, int]:
        """Words per section, in document order, measured on the current bodies."""
        document = await self.assembled()
        return dict(document.words_by_section)

    # --- introspection ----------------------------------------------------

    async def stats(self) -> dict[str, object]:
        """Bounded run-level numbers for the ledger and the status console."""
        states = await self.list_state()
        return {
            "sections": len(states),
            "versions": sum(len(self._storage.read_versions(state.id)) for state in states),
            "snapshots": len(self._storage.read_snapshots()),
            "words": sum(state.words for state in states),
            "final_sections": sum(1 for state in states if state.is_final),
            "locks": asdict(self._locks.stats()),
        }


def _merged_citations(existing: Iterable[CitationRef], text: str) -> tuple[CitationRef, ...]:
    """Re-extract citation markers, keeping any ``source_id`` already resolved.

    Resolution is expensive (it costs a researcher call), so a marker that already
    maps to a card keeps that mapping for as long as the marker survives the edit.
    Markers that are new, or that lost their card, come back unresolved - which is
    what the ``citations_resolve_both_ways`` gate is there to catch.
    """
    resolved = {
        citation.key: citation.source_id for citation in existing if citation.source_id is not None
    }
    return tuple(
        CitationRef(key=key, source_id=resolved.get(key))
        for key in extract_citation_keys(text)[:MAX_CITATIONS]
    )


def _snapshot_rank(snapshot: Snapshot) -> tuple[float, int, datetime]:
    """Sort key for best-so-far: Q, then iteration, then wall-clock time."""
    return (
        snapshot.q if snapshot.q is not None else float("-inf"),
        snapshot.iter if snapshot.iter is not None else -1,
        snapshot.created_at,
    )
