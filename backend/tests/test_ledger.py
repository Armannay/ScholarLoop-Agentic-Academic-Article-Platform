"""Tests for the append-only ledger (:mod:`app.memory.ledger`).

Covers ADR-0002 invariants:

* entries are append-only and cannot be mutated once stored;
* duplicate entry ids and regressive iteration numbers are rejected;
* window splitting cleanly separates ``recent_window`` verbatim entries from older
  entries destined for compaction;
* token totals, completed task ids, failure streaks and issue churn are derived
  accurately;
* JSON Lines (``.jsonl``) checkpoint persistence round-trips without loss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.enums import ORCHESTRATOR_ROLE, AgentRole, ChangeStatus
from app.contracts.ledger import ChangeEntry, EntryRole, TokenUsage, patch_ref_for
from app.memory.errors import DuplicateEntryError, EntryNotFoundError, LedgerError
from app.memory.ledger import InMemoryLedger, LedgerStore
from tests.factories import sample_ulid


def make_entry(
    seed: int,
    *,
    iteration: int = 1,
    task_seed: int | None = None,
    role: EntryRole = AgentRole.WRITER,
    targets: list[str] | None = None,
    summary: str | None = None,
    status: ChangeStatus = ChangeStatus.OK,
    tokens_in: int = 400,
    tokens_out: int = 120,
    tokens_cached: int = 200,
    issues_opened: list[str] | None = None,
    issues_closed: list[str] | None = None,
    with_patch: bool = False,
) -> ChangeEntry:
    """Build a deterministic ChangeEntry for ledger tests."""
    entry_id = sample_ulid(seed)
    task_id = sample_ulid(task_seed if task_seed is not None else seed + 50)
    patch_ref = patch_ref_for(sample_ulid(seed + 100)) if with_patch else None
    return ChangeEntry(
        id=entry_id,
        iter=iteration,
        task_id=task_id,
        role=role,
        targets=targets if targets is not None else ["introduction"],
        summary=summary or f"Entry {seed} drafted target section in iteration {iteration}.",
        patch_ref=patch_ref,
        tokens=TokenUsage(**{"in": tokens_in, "out": tokens_out, "cached": tokens_cached}),
        issues_opened=issues_opened or [],
        issues_closed=issues_closed or [],
        status=status,
    )


def test_ledger_satisfies_protocol() -> None:
    """InMemoryLedger implements the runtime-checkable LedgerStore protocol."""
    ledger = InMemoryLedger()
    assert isinstance(ledger, LedgerStore)
    assert len(ledger) == 0
    assert ledger.count == 0
    assert ledger.latest_iteration == 0


def test_append_and_lookup_by_id() -> None:
    """Appended entries can be retrieved by id; missing ids raise EntryNotFoundError."""
    ledger = InMemoryLedger()
    entry = make_entry(1, iteration=1, with_patch=True)
    stored = ledger.append(entry)

    assert stored == entry
    assert len(ledger) == 1
    assert ledger.get(entry.id) == entry
    assert ledger.require(entry.id) == entry
    assert ledger.get(sample_ulid(99)) is None

    with pytest.raises(EntryNotFoundError) as excinfo:
        ledger.require(sample_ulid(99))
    assert excinfo.value.code == "entry_not_found"


def test_ledger_is_immutable_against_caller_mutation() -> None:
    """Mutating the input or returned ChangeEntry never alters the stored ledger row."""
    ledger = InMemoryLedger()
    entry = make_entry(1, targets=["introduction"], issues_opened=["ISSUE-cite-001"])
    returned = ledger.append(entry)

    entry.targets.append("discussion")
    returned.targets.append("conclusion")
    returned.issues_opened.append("ISSUE-cite-002")

    from_store = ledger.require(entry.id)
    assert from_store.targets == ["introduction"]
    assert from_store.issues_opened == ["ISSUE-cite-001"]


def test_duplicate_entry_id_is_rejected() -> None:
    """The ledger is append-only; reusing an entry ULID is a hard error."""
    ledger = InMemoryLedger()
    entry = make_entry(1, iteration=1)
    ledger.append(entry)

    with pytest.raises(DuplicateEntryError) as excinfo:
        ledger.append(entry)
    assert excinfo.value.code == "duplicate_entry"
    assert len(ledger) == 1


def test_regressive_iteration_is_rejected_by_default() -> None:
    """Appending an entry from an earlier iteration than the latest recorded one fails."""
    ledger = InMemoryLedger()
    ledger.append(make_entry(1, iteration=2))

    with pytest.raises(LedgerError, match="precedes latest recorded iteration"):
        ledger.append(make_entry(2, iteration=1))


def test_filters_by_iteration_role_task_target_and_status() -> None:
    """Entries can be queried along every orchestration dimension."""
    e1 = make_entry(
        1,
        iteration=1,
        task_seed=10,
        role=AgentRole.ARCHITECT,
        targets=["introduction"],
    )
    e2 = make_entry(
        2,
        iteration=1,
        task_seed=11,
        role=AgentRole.RESEARCHER,
        targets=["introduction", "related-work"],
    )
    e3 = make_entry(
        3,
        iteration=2,
        task_seed=12,
        role=AgentRole.WRITER,
        targets=["related-work"],
        status=ChangeStatus.NEEDS_USER_INPUT,
    )
    ledger = InMemoryLedger([e1, e2, e3])

    assert [e.id for e in ledger.by_iteration(1)] == [e1.id, e2.id]
    assert [e.id for e in ledger.by_role(AgentRole.RESEARCHER)] == [e2.id]
    assert [e.id for e in ledger.by_role("writer")] == [e3.id]
    assert [e.id for e in ledger.by_task(sample_ulid(10))] == [e1.id]
    assert [e.id for e in ledger.by_target("related-work")] == [e2.id, e3.id]
    assert [e.id for e in ledger.by_status(ChangeStatus.NEEDS_USER_INPUT)] == [e3.id]
    assert [e.id for e in ledger.needs_user_input_entries()] == [e3.id]
    assert ledger.completed_task_ids() == {sample_ulid(10), sample_ulid(11)}


def test_orchestrator_entries_are_queryable_but_never_complete_a_task() -> None:
    """A rollback is the loop's own action, not evidence that a task was executed."""
    written = make_entry(1, iteration=1, task_seed=10, role=AgentRole.WRITER)
    rollback = make_entry(
        2,
        iteration=1,
        task_seed=11,
        role=ORCHESTRATOR_ROLE,
        status=ChangeStatus.OK,
        tokens_in=0,
        tokens_out=0,
        tokens_cached=0,
    )
    ledger = InMemoryLedger([written, rollback])

    assert [entry.id for entry in ledger.by_role(ORCHESTRATOR_ROLE)] == [rollback.id]
    assert [entry.id for entry in ledger.by_role("orchestrator")] == [rollback.id]
    assert rollback.role_name == ORCHESTRATOR_ROLE
    assert ledger.total_tokens(role=ORCHESTRATOR_ROLE).total == 0
    # Only the writer's task counts as completed.
    assert ledger.completed_task_ids() == {sample_ulid(10)}


def test_window_splits_older_and_recent_entries() -> None:
    """window(recent_window) keeps the tail verbatim and separates older entries."""
    entries = [make_entry(i, iteration=i) for i in range(1, 6)]
    ledger = InMemoryLedger(entries)

    older, recent = ledger.window(3)
    assert [e.id for e in older] == [entries[0].id, entries[1].id]
    assert [e.id for e in recent] == [entries[2].id, entries[3].id, entries[4].id]

    older_all, recent_none = ledger.window(0)
    assert len(older_all) == 5
    assert recent_none == []

    with pytest.raises(LedgerError, match="recent_window"):
        ledger.window(-1)


def test_token_accounting_and_failure_streak() -> None:
    """Token totals sum in/out/cached without overlap, and failed_streak counts tail failures."""
    ledger = InMemoryLedger()
    ledger.append(
        make_entry(
            1,
            iteration=1,
            role=AgentRole.WRITER,
            tokens_in=100,
            tokens_out=50,
            tokens_cached=25,
        )
    )
    ledger.append(
        make_entry(
            2,
            iteration=1,
            role=AgentRole.REVIEWER,
            status=ChangeStatus.FAILED,
            tokens_in=200,
            tokens_out=30,
            tokens_cached=50,
        )
    )
    ledger.append(
        make_entry(
            3,
            iteration=2,
            role=AgentRole.WRITER,
            status=ChangeStatus.FAILED,
            tokens_in=150,
            tokens_out=20,
            tokens_cached=0,
        )
    )

    total = ledger.total_tokens()
    assert (total.in_, total.out, total.cached, total.total) == (450, 100, 75, 625)

    iter1 = ledger.iteration_tokens(1)
    assert (iter1.in_, iter1.out, iter1.cached, iter1.total) == (300, 80, 75, 455)

    writer_tokens = ledger.total_tokens(role=AgentRole.WRITER)
    assert writer_tokens.total == 345
    assert ledger.failed_streak() == 2


def test_issue_summary_tracks_open_closed_reopened_and_churn() -> None:
    """Issues open, close and can be reopened; churn_ratio reflects unresolved share."""
    ledger = InMemoryLedger()
    assert ledger.issue_summary().churn_ratio == 0.0

    ledger.append(
        make_entry(
            1,
            iteration=1,
            role=AgentRole.REVIEWER,
            issues_opened=["ISSUE-cite-001", "ISSUE-len-002", "ISSUE-style-003"],
        )
    )
    ledger.append(
        make_entry(
            2,
            iteration=2,
            role=AgentRole.WRITER,
            issues_closed=["ISSUE-cite-001", "ISSUE-len-002"],
        )
    )
    summary = ledger.issue_summary()
    assert summary.open_issues == ["ISSUE-style-003"]
    assert summary.closed_issues == ["ISSUE-cite-001", "ISSUE-len-002"]
    assert summary.total_opened == 3
    assert summary.total_closed == 2
    assert summary.churn_ratio == pytest.approx(1 / 3)

    # Reopening an issue moves it back to open_issues
    ledger.append(
        make_entry(
            3,
            iteration=3,
            role=AgentRole.VALIDATOR,
            issues_opened=["ISSUE-cite-001"],
        )
    )
    reopened = ledger.issue_summary()
    assert reopened.open_issues == ["ISSUE-style-003", "ISSUE-cite-001"]
    assert reopened.closed_issues == ["ISSUE-len-002"]


def test_jsonl_checkpoint_round_trip(tmp_path: Path) -> None:
    """A ledger streaming to or saved as .jsonl reloads byte-identically."""
    jsonl_path = tmp_path / "run" / "ledger.jsonl"
    live = InMemoryLedger(path=jsonl_path)
    e1 = live.append(make_entry(1, iteration=1, with_patch=True))
    e2 = live.append(make_entry(2, iteration=2, issues_opened=["ISSUE-gap-001"]))

    reloaded = InMemoryLedger.from_jsonl(jsonl_path)
    assert reloaded.entries() == [e1, e2]

    snapshot_path = tmp_path / "snapshot.jsonl"
    reloaded.save_jsonl(snapshot_path)
    from_snapshot = InMemoryLedger.from_jsonl(snapshot_path)
    assert from_snapshot.entries() == [e1, e2]

    bad_path = tmp_path / "corrupt.jsonl"
    bad_path.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="invalid ChangeEntry"):
        InMemoryLedger.from_jsonl(bad_path)

    with pytest.raises(LedgerError, match="not found"):
        InMemoryLedger.from_jsonl(tmp_path / "missing.jsonl")


def test_jsonl_append_failure_does_not_mutate_memory_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If persisting a JSONL line fails, in-memory state stays clean and retry succeeds."""
    jsonl_path = tmp_path / "run" / "ledger.jsonl"
    ledger = InMemoryLedger(path=jsonl_path)

    first = make_entry(1, iteration=1)
    ledger.append(first)
    assert len(ledger) == 1
    assert ledger.latest_iteration == 1
    baseline_bytes = jsonl_path.read_bytes()

    second = make_entry(2, iteration=2)
    original_open = Path.open

    def failing_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self == jsonl_path and args and args[0] == "a":
            raise OSError("simulated disk full during JSONL append")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(LedgerError, match="failed to persist ChangeEntry") as excinfo:
        ledger.append(second)
    assert excinfo.value.code == "ledger_error"

    # In-memory state and on-disk file must not retain the failed entry
    assert len(ledger) == 1
    assert ledger.get(second.id) is None
    assert ledger.latest_iteration == 1
    assert jsonl_path.read_bytes() == baseline_bytes

    # Restoring normal I/O allows retrying the exact same entry without DuplicateEntryError
    monkeypatch.undo()
    stored_second = ledger.append(second)
    assert stored_second == second
    assert len(ledger) == 2
    assert ledger.latest_iteration == 2
    assert InMemoryLedger.from_jsonl(jsonl_path).entries() == [first, second]


def test_extend_mapping_coercion_and_invalid_payloads() -> None:
    """extend() accepts mappings and ChangeEntry objects; invalid payloads raise LedgerError."""
    ledger = InMemoryLedger(enforce_monotonic_iter=False)
    e1 = make_entry(1, iteration=2)
    e2 = make_entry(2, iteration=1)
    stored = ledger.extend([e1.model_dump(by_alias=True), e2])
    assert len(stored) == 2
    assert ledger.latest_iteration == 2

    with pytest.raises(LedgerError, match="invalid ChangeEntry payload"):
        ledger.append({"id": "not-a-ulid"})

    with pytest.raises(LedgerError, match="expected ChangeEntry or mapping"):
        ledger.append("invalid-entry")  # type: ignore[arg-type]
