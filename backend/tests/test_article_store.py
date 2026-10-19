"""Article store tests: versioning, optimistic concurrency, snapshots, rollback.

The two properties this file exists to protect:

* a stale patch is *discarded*, never merged (three parallel writers cannot clobber
  each other), and
* a rollback appends versions instead of rewriting them, so the ledger, the history
  and the snapshot still tell the same story afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.article import (
    AppliedPatch,
    ArticleStore,
    InMemoryStorage,
    PatchRejected,
    Snapshot,
    StalePatch,
)
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import Patch, PatchOp
from app.contracts.section import CitationRef
from app.core.errors import ContractViolation, NotFoundError
from app.core.hashing import content_hash

pytestmark = pytest.mark.article


@dataclass
class Blueprint:
    """A stand-in for ``SectionSpec``: the store only needs an id and a title."""

    id: str
    title: str


@pytest.fixture
def store() -> ArticleStore:
    """An empty store on in-memory storage."""
    return ArticleStore(InMemoryStorage())


async def seeded(store: ArticleStore) -> ArticleStore:
    """Two sections, the first already drafted."""
    await store.create_sections(
        [Blueprint("introduction", "Introduction"), Blueprint("methods", "Methods")]
    )
    await store.patch_section(
        "introduction",
        [
            PatchOp(
                op=PatchOpType.REPLACE_SECTION, text="Loops trade tokens for quality (Smith, 2020)."
            )
        ],
    )
    return store


def patch(section_id: str, base_version: int, *ops: PatchOp) -> Patch:
    """Build a patch the way an agent would."""
    return Patch(section_id=section_id, base_version=base_version, ops=list(ops))


def replace(old: str, new: str) -> PatchOp:
    """A ``replace_text`` op."""
    return PatchOp(op=PatchOpType.REPLACE_TEXT, old=old, new=new)


# --------------------------------------------------------------------------- sections


async def test_create_starts_empty_and_validates_through_the_contract(
    store: ArticleStore,
) -> None:
    """A new section is version 0, empty-hashed and carries no citations."""
    row = await store.create_section("introduction", "Introduction", order=3)
    assert (row.version, row.words, row.status) == (0, 0, SectionStatus.EMPTY)
    assert row.hash == content_hash("")
    assert row.order == 3
    state = row.state()
    assert state.id == "introduction"
    assert state.citations == []


async def test_order_defaults_to_append_order(store: ArticleStore) -> None:
    """Omitting ``order`` appends; equal orders tie-break on the section id."""
    await store.create_section("introduction", "Introduction")
    await store.create_section("methods", "Methods")
    await store.create_section("abstract", "Abstract")
    assert store.section_ids() == ["introduction", "methods", "abstract"]
    assert [state.order for state in await store.list_state()] == [0, 1, 2]
    await store.reorder(["abstract", "introduction", "methods"])
    assert store.section_ids() == ["abstract", "introduction", "methods"]


async def test_create_sections_takes_order_from_position(store: ArticleStore) -> None:
    """``create_sections`` is how the architect lays out the rubric's sections."""
    created = await store.create_sections(
        [Blueprint("introduction", "Introduction"), Blueprint("references", "References")]
    )
    assert [row.order for row in created] == [0, 1]
    assert store.section_ids() == ["introduction", "references"]


async def test_duplicate_and_missing_sections_are_errors(store: ArticleStore) -> None:
    """Ids are unique; reading an unknown section is a 404, not a silent empty."""
    await store.create_section("introduction", "Introduction")
    with pytest.raises(ContractViolation):
        await store.create_section("introduction", "Intro")
    with pytest.raises(NotFoundError):
        await store.get_section("nope")
    with pytest.raises(NotFoundError):
        await store.history("nope")


async def test_bad_section_id_is_rejected_by_the_contract(store: ArticleStore) -> None:
    """Section ids are slugs; the contract, not the store, says so."""
    with pytest.raises(ContractViolation):
        await store.create_section("Not A Slug", "Not a slug")


# --------------------------------------------------------------------------- patching


async def test_apply_patch_bumps_only_its_own_section(store: ArticleStore) -> None:
    """Independent versioning: one section's edit leaves the others alone."""
    await seeded(store)
    result = await store.apply_patch(patch("introduction", 1, replace("quality", "evidence")))
    assert isinstance(result, AppliedPatch)
    assert (result.base_version, result.version, result.changed) == (1, 2, True)
    methods = await store.get_section("methods")
    assert (methods.version, methods.status) == (0, SectionStatus.EMPTY)


async def test_stale_base_version_is_rejected_not_merged(store: ArticleStore) -> None:
    """Optimistic concurrency: a patch authored against v0 cannot land on v1."""
    await seeded(store)
    stale = patch("introduction", 0, replace("quality", "evidence"))
    with pytest.raises(StalePatch) as excinfo:
        await store.apply_patch(stale)
    assert (excinfo.value.base_version, excinfo.value.current_version) == (0, 1)
    assert "re-read the section" in excinfo.value.message
    # Nothing was written: the head is untouched and no version appeared.
    head = await store.get_section("introduction")
    assert head.version == 1
    assert len(await store.history("introduction")) == 1


async def test_stale_patch_of_a_whole_batch_stops_the_batch(store: ArticleStore) -> None:
    """``apply_patches`` checks every base version before it commits any of them."""
    await seeded(store)
    await store.patch_section("methods", [PatchOp(op=PatchOpType.REPLACE_SECTION, text="M.")])
    good = patch("introduction", 1, replace("quality", "evidence"))
    bad = patch("methods", 0, replace("M.", "N."))
    with pytest.raises(StalePatch):
        await store.apply_patches([good, bad])


async def test_whitespace_only_patch_creates_no_version(store: ArticleStore) -> None:
    """A patch that changes no content is reported as unchanged and writes nothing."""
    await seeded(store)
    before = await store.get_section("introduction")
    # Trailing spaces and blank lines vanish in normalisation: no content moved.
    result = await store.apply_patch(
        patch(
            "introduction",
            before.version,
            PatchOp(op=PatchOpType.REPLACE_SECTION, text=f"{before.text}   \n\n"),
        )
    )
    assert result.changed is False
    assert result.version == before.version
    assert result.stats.touched_chars == 0
    assert len(await store.history("introduction")) == before.version


async def test_patch_rejection_surfaces_the_repair_instruction(store: ArticleStore) -> None:
    """The store passes the engine's precise reason straight through."""
    await seeded(store)
    with pytest.raises(PatchRejected) as excinfo:
        await store.apply_patch(patch("introduction", 1, replace("absent", "x")))
    assert "'old' occurs 0 time(s)" in excinfo.value.repair_instruction()
    assert (await store.get_section("introduction")).version == 1


async def test_patch_section_reads_the_head_so_it_cannot_go_stale(
    store: ArticleStore,
) -> None:
    """The read-modify-write path assigns ``base_version`` under the lock."""
    await seeded(store)
    first = await store.patch_section("introduction", [replace("quality", "evidence")])
    second = await store.patch_section("introduction", [replace("evidence", "cost")])
    assert (first.version, second.version) == (2, 3)


# --------------------------------------------------------------------------- history


async def test_history_lists_versions_with_hashes_but_no_bodies(
    store: ArticleStore,
) -> None:
    """History travels by reference: versions, hashes and diff sizes only."""
    await seeded(store)
    await store.patch_section("introduction", [replace("quality", "evidence")])
    history = await store.history("introduction")
    assert [entry.version for entry in history] == [1, 2]
    assert all(entry.hash.startswith("sha256:") for entry in history)
    assert history[1].chars_added > 0
    assert not hasattr(history[0], "text")
    assert await store.version_text("introduction", 1) != await store.version_text(
        "introduction", 2
    )


async def test_history_hashes_are_stable_across_reads(store: ArticleStore) -> None:
    """The same version always reports the same hash, and equal text hashes equal."""
    await seeded(store)
    first = await store.history("introduction")
    second = await store.history("introduction")
    assert [entry.hash for entry in first] == [entry.hash for entry in second]
    assert first[0].hash == content_hash(await store.version_text("introduction", 1))


async def test_diff_between_two_versions(store: ArticleStore) -> None:
    """``diff`` reports both the character stats and a rendered unified diff."""
    await seeded(store)
    await store.patch_section("introduction", [replace("quality", "evidence")])
    result = await store.diff("introduction", 1, 2)
    assert (result.from_version, result.to_version) == (1, 2)
    assert result.diff.stats.chars_added > 0
    assert result.diff.lines_added == 1
    assert "-Loops trade tokens for quality" in result.diff.unified
    with pytest.raises(NotFoundError):
        await store.diff("introduction", 1, 99)


# ---------------------------------------------------------------------------- status


async def test_any_content_edit_resets_the_status_to_draft(store: ArticleStore) -> None:
    """A ``reviewed`` verdict describes text; new text invalidates it."""
    await seeded(store)
    await store.set_status("introduction", SectionStatus.REVIEWED)
    assert (await store.get_section("introduction")).status is SectionStatus.REVIEWED
    await store.patch_section("introduction", [replace("quality", "evidence")])
    assert (await store.get_section("introduction")).status is SectionStatus.DRAFT


async def test_replace_section_is_refused_once_reviewed(store: ArticleStore) -> None:
    """Whole-section rewrites are for drafts; signed-off text needs surgical ops."""
    await seeded(store)
    await store.set_status("introduction", SectionStatus.FINAL)
    with pytest.raises(PatchRejected) as excinfo:
        await store.patch_section(
            "introduction", [PatchOp(op=PatchOpType.REPLACE_SECTION, text="Rewritten.")]
        )
    assert "draft or empty" in excinfo.value.repair_instruction()
    # Demoting re-opens the section for a rewrite.
    await store.set_status("introduction", SectionStatus.DRAFT)
    result = await store.patch_section(
        "introduction", [PatchOp(op=PatchOpType.REPLACE_SECTION, text="Rewritten.")]
    )
    assert result.changed is True


async def test_status_rules_reject_nonsense(store: ArticleStore) -> None:
    """An empty section cannot be reviewed, and nothing can be set back to empty."""
    await seeded(store)
    with pytest.raises(ContractViolation):
        await store.set_status("methods", SectionStatus.REVIEWED)
    with pytest.raises(ContractViolation):
        await store.set_status("introduction", SectionStatus.EMPTY)


async def test_issues_and_titles_are_mutable_metadata(store: ArticleStore) -> None:
    """Issue lists and titles change without touching content or version."""
    await seeded(store)
    row = await store.set_open_issues("introduction", ["ISSUE-citations-001"])
    assert row.open_issues == ("ISSUE-citations-001",)
    assert row.version == 1
    renamed = await store.rename_section("introduction", "Introduction and Motivation")
    assert renamed.title == "Introduction and Motivation"
    with pytest.raises(ContractViolation):
        await store.set_open_issues("introduction", ["not a valid issue id!!"])


# ------------------------------------------------------------------------- citations


async def test_citations_are_extracted_and_resolution_survives_edits(
    store: ArticleStore,
) -> None:
    """Markers come from the text; a resolved card stays resolved across an edit."""
    await seeded(store)
    row = await store.get_section("introduction")
    assert [citation.key for citation in row.citations] == ["(Smith, 2020)"]
    assert row.citations[0].source_id is None

    resolved = await store.resolve_citation("introduction", "(Smith, 2020)", "0" * 26)
    assert resolved.citations[0].is_resolved

    edited = await store.patch_section("introduction", [replace("quality", "evidence")])
    after = await store.get_section("introduction")
    assert edited.version == after.version
    assert after.citations[0].source_id == "0" * 26

    dropped = await store.patch_section("introduction", [replace(" (Smith, 2020).", ".")])
    assert dropped.version == after.version + 1
    assert (await store.get_section("introduction")).citations == ()


async def test_numeric_markers_and_unknown_keys(store: ArticleStore) -> None:
    """Numeric citation styles are extracted too; unknown keys cannot be resolved."""
    await store.create_section("results", "Results", text="A result [1] and another [2].")
    keys = [citation.key for citation in (await store.get_section("results")).citations]
    assert keys == ["[1]", "[2]"]
    with pytest.raises(NotFoundError):
        await store.resolve_citation("results", "[3]", "0" * 26)
    updated = await store.set_citations("results", [CitationRef(key="[1]", source_id=None)])
    assert [citation.key for citation in updated.citations] == ["[1]"]


# -------------------------------------------------------------------------- snapshots


async def test_snapshot_and_restore_round_trip(store: ArticleStore) -> None:
    """Rolling back to the best snapshot returns the exact bodies and states."""
    await seeded(store)
    good = await store.snapshot("best", q=78.0, iter=2)
    good_text = await store.get_text("introduction")

    # Erode: a rewrite that makes the section worse.
    await store.patch_section(
        "introduction", [PatchOp(op=PatchOpType.REPLACE_SECTION, text="Worse. Much worse.")]
    )
    assert await store.get_text("introduction") != good_text

    result = await store.restore(good.id)
    assert result.restored == ("introduction",)
    assert result.unchanged == ("methods",)
    assert result.q == 78.0

    restored = await store.get_section("introduction")
    assert restored.text == good_text
    assert restored.hash == content_hash(good_text)
    assert restored.status is SectionStatus.DRAFT
    # The rollback appended a version; the eroded one is still in the history.
    assert restored.version == 3
    assert [entry.version for entry in await store.history("introduction")] == [1, 2, 3]
    assert await store.version_text("introduction", 2) == "Worse. Much worse."
    assert result.versions == {"introduction": 3}


async def test_restore_is_idempotent(store: ArticleStore) -> None:
    """Restoring twice changes nothing the second time."""
    await seeded(store)
    snapshot = await store.snapshot("s", q=50.0)
    first = await store.restore(snapshot.id)
    second = await store.restore(snapshot.id)
    assert first.restored == ()
    assert second.restored == ()
    assert second.unchanged == ("introduction", "methods")


async def test_restore_recreates_a_section_deleted_after_the_snapshot(
    store: ArticleStore,
) -> None:
    """A rollback is not a delete, but it does bring back what the snapshot held."""
    await seeded(store)
    snapshot = await store.snapshot("with-methods", q=60.0)
    await store.delete_section("methods")
    assert store.section_ids() == ["introduction"]
    result = await store.restore(snapshot.id)
    # The row is back; it is "unchanged" because the snapshot body was empty too.
    assert store.section_ids() == ["introduction", "methods"]
    assert result.unchanged == ("introduction", "methods")
    assert (await store.get_section("methods")).title == "Methods"


async def test_snapshot_captures_state_and_body(store: ArticleStore) -> None:
    """A snapshot is a full freeze: order, title, status, words and text."""
    await seeded(store)
    snapshot = await store.snapshot("after-iter-1", q=64.5, iter=1)
    assert isinstance(snapshot, Snapshot)
    assert snapshot.section_ids == ("introduction", "methods")
    assert snapshot.total_words == 7
    frozen = snapshot.state_of("introduction")
    assert frozen is not None
    assert frozen.status is SectionStatus.DRAFT
    assert snapshot.text_of("introduction") == await store.get_text("introduction")
    assert snapshot.text_of("nope") is None


async def test_best_snapshot_prefers_the_highest_q(store: ArticleStore) -> None:
    """Best-so-far is about measured quality, not recency."""
    await seeded(store)
    await store.snapshot("early-high", q=81.0, iter=1)
    await store.snapshot("late-low", q=72.0, iter=5)
    best = await store.best_snapshot()
    assert best is not None
    assert best.label == "early-high"


async def test_best_snapshot_breaks_ties_on_the_later_iteration(
    store: ArticleStore,
) -> None:
    """Equal Q: prefer the later iteration, so a rollback keeps the newer text."""
    await seeded(store)
    await store.snapshot("first", q=75.0, iter=2)
    await store.snapshot("second", q=75.0, iter=4)
    best = await store.best_snapshot()
    assert best is not None
    assert best.label == "second"


async def test_unscored_snapshots_are_never_best(store: ArticleStore) -> None:
    """Without a Q there is no "best"; an unscored snapshot only is ``None``."""
    await seeded(store)
    assert await store.best_snapshot() is None
    await store.snapshot("no-q")
    assert await store.best_snapshot() is None
    scored = await store.snapshot("scored", q=10.0)
    best = await store.best_snapshot()
    assert best is not None and best.id == scored.id
    assert len(await store.snapshots()) == 2


async def test_restore_of_an_unknown_snapshot_is_a_404(store: ArticleStore) -> None:
    """Rolling back to something that does not exist says so."""
    await seeded(store)
    with pytest.raises(NotFoundError):
        await store.restore("01ARZ3NDEKTSV4RRFFQ69G5FAV")


# --------------------------------------------------------------------------- quality


async def test_record_quality_stamps_each_head_once(store: ArticleStore) -> None:
    """One Q per article measurement; a head already scored is left alone."""
    await seeded(store)
    assert await store.record_quality(70.0) == 1
    assert await store.record_quality(90.0) == 0
    await store.patch_section("introduction", [replace("quality", "evidence")])
    assert await store.record_quality(80.0) == 1
    history = await store.history("introduction")
    assert [entry.q_after for entry in history] == [70.0, 80.0]


async def test_store_exposes_the_erosion_signals(store: ArticleStore) -> None:
    """The detectors read the same history the ledger does."""
    await seeded(store)
    await store.record_quality(70.0)
    await store.patch_section("introduction", [replace("quality", "cost")])
    await store.record_quality(65.0)
    assert await store.hash_recurred("introduction", window=3) is False
    assert await store.edits_without_gain("introduction") == 1

    # Round-trip back to the v1 body: the hash recurs inside the window.
    await store.patch_section("introduction", [replace("cost", "quality")])
    assert await store.hash_recurred("introduction", window=3) is True
    assert (await store.history("introduction"))[-1].hash == (await store.history("introduction"))[
        0
    ].hash


# -------------------------------------------------------------------------- assembly


async def test_export_writes_the_whole_article(store: ArticleStore, tmp_path: Path) -> None:
    """``export_markdown`` writes ordered sections plus the rendered reference list."""
    await seeded(store)
    await store.create_section("references", "References")
    target = tmp_path / "out" / "article.md"
    written = await store.export_markdown(
        target, title="Token-frugal loops", references=["Smith, J. (2020). Loops. Journal."]
    )
    assert written == target
    markdown = target.read_text(encoding="utf-8")
    assert markdown.startswith("# Token-frugal loops\n\n## Introduction\n\n")
    assert markdown.index("## Introduction") < markdown.index("## Methods")
    assert markdown.index("## Methods") < markdown.index("## References")
    assert "1. Smith, J. (2020). Loops. Journal." in markdown

    counts = await store.word_counts()
    assert counts["introduction"] == 7
    assert counts["methods"] == 0


async def test_store_stats_are_bounded_numbers(store: ArticleStore) -> None:
    """The status console reads one dict, not the article."""
    await seeded(store)
    await store.snapshot("s", q=55.0)
    stats = await store.stats()
    assert stats["sections"] == 2
    assert stats["versions"] == 1
    assert stats["snapshots"] == 1
    assert stats["words"] == 7
    assert stats["final_sections"] == 0


# ----------------------------------------------------------------- storage backends


class RecordingStorage:
    """A third-party backend: it only has to satisfy the ``SectionStorage`` protocol.

    Stands in for the SQLAlchemy implementation the next step will write, and proves
    the store is coupled to the protocol rather than to the in-memory dicts.
    """

    def __init__(self) -> None:
        self._inner = InMemoryStorage()
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        method = getattr(self._inner, name)

        def record(*args: object, **kwargs: object) -> object:
            self.calls.append(name)
            return method(*args, **kwargs)

        return record


async def test_a_custom_backend_only_needs_the_protocol(store: ArticleStore) -> None:
    """Swap the storage: the store behaves identically through the protocol."""
    storage = RecordingStorage()
    custom = ArticleStore(storage)
    await custom.create_section("introduction", "Introduction", text="Hello (Smith, 2020).")
    assert (await custom.get_section("introduction")).version == 1
    assert len(await custom.history("introduction")) == 1
    assert {"write_section", "append_version", "read_section"} <= set(storage.calls)


async def test_restore_brings_back_a_final_status(store: ArticleStore) -> None:
    """Rolling back to a signed-off checkpoint restores the sign-off, not just the text."""
    await seeded(store)
    await store.set_status("introduction", SectionStatus.FINAL)
    snapshot = await store.snapshot("signed-off", q=88.0)
    await store.patch_section("introduction", [replace("quality", "noise")])
    assert (await store.get_section("introduction")).status is SectionStatus.DRAFT

    await store.restore(snapshot.id)
    restored = await store.get_section("introduction")
    assert restored.status is SectionStatus.FINAL
    assert restored.version == 3
