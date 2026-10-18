"""Domain errors for the ScholarLoop memory layer.

Kept in a leaf module so :mod:`app.memory.ledger`, :mod:`app.memory.sources`,
:mod:`app.memory.compactor` and :mod:`app.memory.digest` can all raise and catch
them without introducing circular imports. Every error inherits from
:class:`~app.core.errors.ScholarLoopError` and carries a stable ``code`` for logs
and API responses.
"""

from __future__ import annotations

from app.core.errors import NotFoundError, ScholarLoopError

__all__ = [
    "CompactionError",
    "DigestBudgetExceeded",
    "DigestError",
    "DuplicateEntryError",
    "EntryNotFoundError",
    "LedgerError",
    "MemoryError",
    "SourceConflictError",
    "SourceNotFoundError",
    "SourceStoreError",
    "SourceVerificationError",
]


class MemoryError(ScholarLoopError):
    """Base class for every memory-layer failure."""

    code = "memory_error"
    http_status = 500


class LedgerError(MemoryError):
    """The append-only ledger rejected an invalid write or state transition."""

    code = "ledger_error"
    http_status = 409


class DuplicateEntryError(LedgerError):
    """A ChangeEntry with the same ULID already exists in the append-only ledger."""

    code = "duplicate_entry"
    http_status = 409


class EntryNotFoundError(NotFoundError):
    """No ChangeEntry with the requested id exists in the ledger."""

    code = "entry_not_found"
    http_status = 404


class SourceStoreError(MemoryError):
    """Base class for source-card store failures."""

    code = "source_store_error"
    http_status = 400


class SourceNotFoundError(NotFoundError):
    """No SourceCard with the requested id exists in the source store."""

    code = "source_not_found"
    http_status = 404


class SourceConflictError(SourceStoreError):
    """A SourceCard conflicts with an existing card in the store."""

    code = "source_conflict"
    http_status = 409


class SourceVerificationError(SourceStoreError):
    """An unauthorised role or evidence-free call tried to verify a SourceCard."""

    code = "source_verification_error"
    http_status = 403


class CompactionError(MemoryError):
    """History compaction could not produce a valid bounded summary."""

    code = "compaction_error"
    http_status = 502


class DigestError(MemoryError):
    """Base class for digest assembly or persistence failures."""

    code = "digest_error"
    http_status = 500


class DigestBudgetExceeded(DigestError):
    """The rendered claude.md digest exceeds the policy token ceiling."""

    code = "digest_budget_exceeded"
    http_status = 422
