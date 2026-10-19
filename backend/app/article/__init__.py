"""The article: independently versioned sections, patches, snapshots, assembly.

This package owns the only mutable state in ScholarLoop that a reader cares about -
the article itself. Everything else (ledger, digest, plan) is metadata about it.

Layout:

* :mod:`app.article.store` - :class:`~app.article.store.ArticleStore`: sections with
  their own version lists, optimistic concurrency, snapshots and best-so-far.
* :mod:`app.article.patch` - the pure patch engine and its two failure modes,
  :class:`~app.article.patch.StalePatch` and :class:`~app.article.patch.PatchRejected`.
* :mod:`app.article.locks` - per-section async locks with timeouts and a global
  acquisition order, so parallel agents edit different sections without waiting.
* :mod:`app.article.assemble` - ordered sections plus the reference list into one
  markdown document, with the word counts the gates measure.
* :mod:`app.article.recurrence` - the two erosion signals the loop controller reads.

Only :mod:`app.contracts` and :mod:`app.core` are imported from the rest of the
codebase, so the store can be exercised with no provider, no network and no database.
"""

from __future__ import annotations

from app.article.assemble import (
    REFERENCE_HEADING,
    REFERENCE_SECTION_ID,
    AssembledArticle,
    AssembledSection,
    assemble,
    assemble_markdown,
    export_markdown,
    per_section_words,
    section_from,
    sort_sections,
    total_words,
)
from app.article.locks import (
    DEFAULT_LOCK_TIMEOUT,
    LockReentrancy,
    LockStats,
    LockTimeout,
    SectionLocks,
    order_section_ids,
)
from app.article.patch import (
    REPLACE_SECTION_ALLOWED_STATUSES,
    DiffStats,
    PatchError,
    PatchOutcome,
    PatchProblem,
    PatchRejected,
    StalePatch,
    TextDiff,
    apply_ops,
    diff_stats,
    diff_texts,
    validate_ops,
)
from app.article.recurrence import (
    Versioned,
    edits_without_gain,
    hash_recurred,
    recurred_hash,
    recurred_sections,
    scored_versions,
)
from app.article.store import (
    MAX_CITATIONS,
    AppliedPatch,
    ArticleStore,
    DiffResult,
    InMemoryStorage,
    RestoreResult,
    SectionBlueprint,
    SectionStorage,
    Snapshot,
    SnapshotSection,
    StoredSection,
    StoredVersion,
    VersionInfo,
    extract_citation_keys,
)

__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "MAX_CITATIONS",
    "REFERENCE_HEADING",
    "REFERENCE_SECTION_ID",
    "REPLACE_SECTION_ALLOWED_STATUSES",
    "AppliedPatch",
    "ArticleStore",
    "AssembledArticle",
    "AssembledSection",
    "DiffResult",
    "DiffStats",
    "InMemoryStorage",
    "LockReentrancy",
    "LockStats",
    "LockTimeout",
    "PatchError",
    "PatchOutcome",
    "PatchProblem",
    "PatchRejected",
    "RestoreResult",
    "SectionBlueprint",
    "SectionLocks",
    "SectionStorage",
    "Snapshot",
    "SnapshotSection",
    "StalePatch",
    "StoredSection",
    "StoredVersion",
    "TextDiff",
    "VersionInfo",
    "Versioned",
    "apply_ops",
    "assemble",
    "assemble_markdown",
    "diff_stats",
    "diff_texts",
    "edits_without_gain",
    "export_markdown",
    "extract_citation_keys",
    "hash_recurred",
    "order_section_ids",
    "per_section_words",
    "recurred_hash",
    "recurred_sections",
    "scored_versions",
    "section_from",
    "sort_sections",
    "total_words",
    "validate_ops",
]
