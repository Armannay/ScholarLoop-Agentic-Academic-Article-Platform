"""Recurrence helpers for the loop's erosion detectors (Step 9).

Both signals answer one question the Q score alone cannot: *is the article still
moving forward, or is it re-writing the same paragraphs?*

* :func:`hash_recurred` - the section's current body is byte-identical to a body it
  already had inside the look-back window, i.e. the last edits round-tripped.
* :func:`edits_without_gain` - how many consecutive measured edits failed to raise
  the best Q seen so far. This is the section-level twin of the plateau rule in
  ``config/policies.yaml``.

The functions are pure and know nothing about storage: they read the
:class:`Versioned` protocol, which the store's version rows satisfy. Step 9 can call
them from a ``QualityReport`` handler, a test can call them from a list of stubs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

__all__ = [
    "Versioned",
    "edits_without_gain",
    "hash_recurred",
    "recurred_hash",
    "recurred_sections",
    "scored_versions",
]


@runtime_checkable
class Versioned(Protocol):
    """The three facts the detectors need from any version row.

    Read-only properties on purpose: the store's version rows are frozen dataclasses,
    and a mutable protocol attribute would not accept them.
    """

    @property
    def version(self) -> int:
        """Monotonic per-section version number."""
        ...

    @property
    def hash(self) -> str:
        """``sha256:<hex>`` of the exact body this version stored."""
        ...

    @property
    def q_after(self) -> float | None:
        """The article Q measured while this version was the head, if it was measured."""
        ...


def _ordered(versions: Sequence[Versioned]) -> list[Versioned]:
    """Versions oldest-first. Callers may pass them in any order."""
    return sorted(versions, key=lambda version: version.version)


def hash_recurred(versions: Sequence[Versioned], *, window: int) -> bool:
    """True when the newest body repeats a body already written inside ``window``.

    ``window`` counts versions, the head included, so ``window=3`` compares the head
    against the two versions before it. A window below 2 can never recur.
    """
    return recurred_hash(versions, window=window) is not None


def recurred_hash(versions: Sequence[Versioned], *, window: int) -> str | None:
    """The hash that recurred, for ``LoopDecision.evidence``; ``None`` when it did not."""
    ordered = _ordered(versions)
    if window < 2 or len(ordered) < 2:
        return None
    lookback = ordered[-window:]
    head = lookback[-1]
    for earlier in lookback[:-1]:
        if earlier.hash == head.hash:
            return head.hash
    return None


def scored_versions(versions: Sequence[Versioned]) -> list[tuple[int, float]]:
    """``(version, Q)`` pairs that actually carry a measurement, oldest-first.

    Unmeasured versions are dropped rather than treated as zero: a version nobody has
    scored yet says nothing about progress, and inventing a 0 would look like a
    catastrophic regression.
    """
    scored: list[tuple[int, float]] = []
    for version in _ordered(versions):
        q = version.q_after
        if q is not None:
            scored.append((version.version, float(q)))
    return scored


def edits_without_gain(versions: Sequence[Versioned], *, min_gain: float = 0.0) -> int:
    """Consecutive measured edits at the end of the history that gained nothing.

    "Gain" means Q strictly above the best Q seen so far by more than ``min_gain``
    (the ``plateau.min_gain`` knob). A sub-threshold improvement does not reset the
    counter and does not raise the bar, so a run of +0.1 wobbles still reads as
    erosion. Returns 0 when nothing has been measured yet.
    """
    scored = scored_versions(versions)
    if not scored:
        return 0
    best = scored[0][1]
    without_gain = 0
    for _, q in scored[1:]:
        if q > best + min_gain:
            best = q
            without_gain = 0
        else:
            without_gain += 1
    return without_gain


def recurred_sections(histories: Mapping[str, Sequence[Versioned]], *, window: int) -> list[str]:
    """Section ids whose head body recurred, sorted. One call for the whole article."""
    return sorted(
        section_id
        for section_id, versions in histories.items()
        if hash_recurred(versions, window=window)
    )
