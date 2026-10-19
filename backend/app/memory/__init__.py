"""ScholarLoop memory layer: append-only ledger, source store, compactor, digest.

Four pillars (ADR-0002, HARD RULES 1 & 3):

* :mod:`app.memory.ledger` — append-only :class:`~app.contracts.ledger.ChangeEntry`
  store with defensive copies, window splitting, token totals, issue churn and
  JSONL checkpointing;
* :mod:`app.memory.sources` — :class:`~app.contracts.source.SourceCard` store with
  normalised DOI/URL deduplication and validator-only verification transitions;
* :mod:`app.memory.compactor` — LLM-assisted (``BindingRole.COMPACTOR``) and
  deterministic compaction of older ledger history that preserves entity ids and
  statuses;
* :mod:`app.memory.digest` — bounded ``claude.md`` digest builder (capped at
  ``policies.digest_max_tokens``) and the :class:`MemoryStore` coordinator.
"""

from __future__ import annotations

from app.memory.compactor import (
    COMPACTOR_JSON_SCHEMA,
    COMPACTOR_SYSTEM_PROMPT,
    CompactedBatch,
    CompactorOutput,
    DigestCompactor,
    deterministic_compact,
)
from app.memory.digest import (
    DEFAULT_DIGEST_FILENAME,
    DigestBuilder,
    DigestSnapshot,
    MemoryStore,
    RunMemory,
)
from app.memory.errors import (
    CompactionError,
    DigestBudgetExceeded,
    DigestError,
    DuplicateEntryError,
    EntryNotFoundError,
    LedgerError,
    MemoryError,
    SourceConflictError,
    SourceNotFoundError,
    SourceStoreError,
    SourceVerificationError,
)
from app.memory.ledger import (
    ChangeLedger,
    InMemoryLedger,
    IssueSummary,
    LedgerStore,
    sum_token_usage,
)
from app.memory.sources import InMemorySourceStore, SourceStore

__all__ = [
    "COMPACTOR_JSON_SCHEMA",
    "COMPACTOR_SYSTEM_PROMPT",
    "DEFAULT_DIGEST_FILENAME",
    "ChangeLedger",
    "CompactedBatch",
    "CompactionError",
    "CompactorOutput",
    "DigestBudgetExceeded",
    "DigestBuilder",
    "DigestCompactor",
    "DigestError",
    "DigestSnapshot",
    "DuplicateEntryError",
    "EntryNotFoundError",
    "InMemoryLedger",
    "InMemorySourceStore",
    "IssueSummary",
    "LedgerError",
    "LedgerStore",
    "MemoryError",
    "MemoryStore",
    "RunMemory",
    "SourceConflictError",
    "SourceNotFoundError",
    "SourceStore",
    "SourceStoreError",
    "SourceVerificationError",
    "deterministic_compact",
    "sum_token_usage",
]
