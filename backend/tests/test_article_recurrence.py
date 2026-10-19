"""Recurrence helper tests: the two erosion signals Step 9 reads.

These are pure functions over version rows, so the tests build the histories
directly - including the shapes that matter most: a body that round-tripped, and a
run of edits that measured no gain.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.article import (
    ArticleStore,
    InMemoryStorage,
    edits_without_gain,
    hash_recurred,
    recurred_hash,
    recurred_sections,
    scored_versions,
)
from app.contracts.enums import PatchOpType
from app.contracts.patch import PatchOp
from app.core.hashing import content_hash

pytestmark = pytest.mark.article


@dataclass(frozen=True)
class Row:
    """A stand-in for ``VersionInfo``: the three facts the protocol requires."""

    version: int
    hash: str
    q_after: float | None = None


def row(version: int, text: str, q: float | None = None) -> Row:
    """A version row keyed by the hash of its body."""
    return Row(version=version, hash=content_hash(text), q_after=q)


def test_no_history_never_recurs() -> None:
    """Fewer than two versions, or a window below 2: nothing can have recurred."""
    assert hash_recurred([], window=3) is False
    assert hash_recurred([row(1, "A")], window=3) is False
    assert hash_recurred([row(1, "A"), row(2, "A")], window=1) is False


def test_a_round_tripped_body_recurs() -> None:
    """A -> B -> A inside the window is the classic erosion shape."""
    history = [row(1, "A"), row(2, "B"), row(3, "A")]
    assert hash_recurred(history, window=3) is True
    assert recurred_hash(history, window=3) == content_hash("A")


def test_a_window_too_small_misses_the_recurrence() -> None:
    """``window`` counts versions including the head, so 2 looks back one edit."""
    history = [row(1, "A"), row(2, "B"), row(3, "A")]
    assert hash_recurred(history, window=2) is False
    assert hash_recurred(history, window=4) is True


def test_forward_progress_does_not_recur() -> None:
    """New text every time is not recurrence, however similar it looks."""
    history = [row(1, "A"), row(2, "B"), row(3, "C")]
    assert hash_recurred(history, window=5) is False
    assert recurred_hash(history, window=5) is None


def test_only_the_head_is_compared() -> None:
    """An old repeat that the head has moved past is not a current recurrence."""
    history = [row(1, "A"), row(2, "A"), row(3, "B"), row(4, "C")]
    assert hash_recurred(history, window=4) is False


def test_versions_may_arrive_in_any_order() -> None:
    """The helpers sort by version; a shuffled history reads the same."""
    shuffled = [row(3, "A"), row(1, "A"), row(2, "B")]
    assert hash_recurred(shuffled, window=3) is True


# ------------------------------------------------------------------ edits without gain


def test_unmeasured_history_reports_zero() -> None:
    """No Q yet means no evidence of erosion - never invent a zero."""
    assert edits_without_gain([row(1, "A"), row(2, "B")]) == 0
    assert edits_without_gain([]) == 0
    assert scored_versions([row(1, "A"), row(2, "B", q=60.0)]) == [(2, 60.0)]


def test_gains_reset_the_counter() -> None:
    """Each real improvement restarts the count."""
    history = [row(1, "A", q=60.0), row(2, "B", q=70.0), row(3, "C", q=80.0)]
    assert edits_without_gain(history) == 0


def test_a_run_of_no_gain_is_counted() -> None:
    """Flat then falling: three edits, none of them progress."""
    history = [
        row(1, "A", q=70.0),
        row(2, "B", q=70.0),
        row(3, "C", q=65.0),
        row(4, "D", q=66.0),
    ]
    assert edits_without_gain(history) == 3


def test_min_gain_sets_the_bar() -> None:
    """Sub-threshold wobbles are erosion, not progress, and do not raise the bar."""
    history = [row(1, "A", q=70.0), row(2, "B", q=70.4), row(3, "C", q=70.8)]
    assert edits_without_gain(history) == 0
    assert edits_without_gain(history, min_gain=1.0) == 2


def test_a_late_gain_ends_the_streak() -> None:
    """Only the trailing run counts."""
    history = [
        row(1, "A", q=70.0),
        row(2, "B", q=60.0),
        row(3, "C", q=55.0),
        row(4, "D", q=90.0),
    ]
    assert edits_without_gain(history) == 0


def test_recurred_sections_covers_the_whole_article() -> None:
    """One call answers "which sections are round-tripping?" for Step 9."""
    histories = {
        "introduction": [row(1, "A"), row(2, "B"), row(3, "A")],
        "methods": [row(1, "M"), row(2, "N"), row(3, "O")],
        "discussion": [row(1, "X"), row(2, "X")],
    }
    assert recurred_sections(histories, window=3) == ["discussion", "introduction"]


# ------------------------------------------------------------------- store integration


async def test_the_signals_read_the_real_history() -> None:
    """The store's own versions satisfy the protocol the detectors expect."""
    store = ArticleStore(InMemoryStorage())
    await store.create_section("discussion", "Discussion", text="First draft.")
    await store.record_quality(70.0)

    await store.patch_section(
        "discussion", [PatchOp(op=PatchOpType.REPLACE_TEXT, old="First", new="Second")]
    )
    await store.record_quality(68.0)
    assert await store.hash_recurred("discussion", window=3) is False
    assert await store.edits_without_gain("discussion") == 1

    await store.patch_section(
        "discussion", [PatchOp(op=PatchOpType.REPLACE_TEXT, old="Second", new="First")]
    )
    assert await store.record_quality(67.0) == 1
    assert await store.hash_recurred("discussion", window=3) is True
    assert recurred_hash(await store.history("discussion"), window=3) is not None
    # Still no gain since the first measurement: the streak grew with the round trip.
    assert await store.edits_without_gain("discussion") == 2
