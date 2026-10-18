"""Tests for the source card store (:mod:`app.memory.sources`).

Covers HARD RULE 1 and role-boundary invariants:

* source cards are deduplicated by normalised DOI or canonical URL so existing ULIDs
  are reused across iterations;
* only ``AgentRole.VALIDATOR`` may set or transition ``verification`` to ``verified``
  or ``mismatch``;
* re-ingesting an ``unknown`` card from the researcher never downgrades a card the
  validator already marked ``verified``;
* ``verified_citation_ratio`` accounts for unresolved citation ids honestly.
"""

from __future__ import annotations

import pytest

from app.contracts.enums import AgentRole, VerificationStatus
from app.contracts.source import SourceCard
from app.memory.errors import (
    SourceConflictError,
    SourceNotFoundError,
    SourceStoreError,
    SourceVerificationError,
)
from app.memory.sources import InMemorySourceStore, SourceStore
from tests.factories import sample_ulid


def make_card(
    seed: int,
    *,
    title: str | None = None,
    doi: str | None = "10.1234/scholarloop.2025.01",
    url: str | None = "https://example.org/papers/1",
    verification: VerificationStatus = VerificationStatus.UNKNOWN,
) -> SourceCard:
    """Build a valid SourceCard for tests."""
    return SourceCard(
        id=sample_ulid(seed),
        title=title or f"Deterministic Agentic Loops Paper {seed}",
        authors=["Nguyen, L.", "Alvarez, R."],
        year=2025,
        venue="Journal of Agentic Systems",
        doi=doi,
        url=url,
        snippet="Bounded digests keep planner prompt tokens constant across iterations.",
        verification=verification,
    )


def test_source_store_satisfies_protocol() -> None:
    """InMemorySourceStore implements the runtime-checkable SourceStore protocol."""
    store = InMemorySourceStore()
    assert isinstance(store, SourceStore)
    assert len(store) == 0


def test_deduplicates_by_normalised_doi_and_preserves_ulid() -> None:
    """Different spellings of the same DOI resolve to the first card's ULID."""
    store = InMemorySourceStore()
    first = make_card(1, doi="https://doi.org/10.1234/JANS.2025.0042", url=None)
    second = make_card(2, doi="DOI: 10.1234/jans.2025.0042", url="https://example.org/p/42")

    stored_first = store.add(first, role=AgentRole.RESEARCHER)
    stored_second = store.add(second, role=AgentRole.RESEARCHER)

    assert len(store) == 1
    assert stored_second.id == stored_first.id
    assert stored_second.url == "https://example.org/p/42"
    found = store.find_by_doi("https://dx.doi.org/10.1234/JANS.2025.0042")
    assert found is not None and found.id == stored_first.id


def test_deduplicates_by_url_when_doi_is_absent() -> None:
    """URL-only cards deduplicate on canonical URL (ignoring trailing slash)."""
    store = InMemorySourceStore()
    c1 = make_card(1, doi=None, url="https://example.org/reports/2025/")
    c2 = make_card(2, doi=None, url="https://example.org/reports/2025")

    s1 = store.add(c1, role=AgentRole.RESEARCHER)
    s2 = store.add(c2, role=AgentRole.RESEARCHER)

    assert len(store) == 1
    assert s1.id == s2.id
    assert store.find_by_url("https://example.org/reports/2025/") is not None


def test_only_validator_may_submit_or_set_verified_status() -> None:
    """Non-validator roles are forbidden from setting verification!=unknown."""
    store = InMemorySourceStore()
    forged = make_card(1, verification=VerificationStatus.VERIFIED)

    for forbidden_role in (
        AgentRole.RESEARCHER,
        AgentRole.WRITER,
        AgentRole.REVIEWER,
        AgentRole.ARCHITECT,
    ):
        with pytest.raises(SourceVerificationError) as excinfo:
            store.add(forged, role=forbidden_role)
        assert excinfo.value.code == "source_verification_error"

    unknown_card = store.add(make_card(2), role=AgentRole.RESEARCHER)
    with pytest.raises(SourceVerificationError):
        store.set_verification(
            unknown_card.id,
            VerificationStatus.VERIFIED,
            role=AgentRole.RESEARCHER,
        )

    verified = store.set_verification(
        unknown_card.id,
        VerificationStatus.VERIFIED,
        role=AgentRole.VALIDATOR,
        evidence="CrossRef DOI metadata matches title, authors and year.",
    )
    assert verified.verification is VerificationStatus.VERIFIED
    assert (
        store.verification_evidence(unknown_card.id)
        == "CrossRef DOI metadata matches title, authors and year."
    )


def test_researcher_reingest_never_downgrades_verified_card() -> None:
    """Once verified by the validator, a card stays verified if re-carded as unknown."""
    store = InMemorySourceStore()
    card = store.add(make_card(1, doi="10.1000/verified.1"), role=AgentRole.RESEARCHER)
    store.set_verification(
        card.id,
        VerificationStatus.VERIFIED,
        role=AgentRole.VALIDATOR,
        evidence="DOI resolved.",
    )

    re_emitted = make_card(
        2,
        doi="https://doi.org/10.1000/VERIFIED.1",
        verification=VerificationStatus.UNKNOWN,
    )
    merged = store.add(re_emitted, role=AgentRole.RESEARCHER)
    assert merged.id == card.id
    assert merged.verification is VerificationStatus.VERIFIED


def test_conflicting_card_with_same_id_is_rejected() -> None:
    """Reusing a card ULID for a different paper raises SourceConflictError."""
    store = InMemorySourceStore()
    c1 = make_card(1, title="Paper Alpha", doi="10.1000/alpha.1")
    c2 = make_card(1, title="Paper Beta", doi="10.1000/beta.2")
    store.add(c1)

    with pytest.raises(SourceConflictError):
        store.add(c2)


def test_verified_citation_ratio_and_filtering() -> None:
    """verified_citation_ratio handles global store, subset ids and unresolved ids."""
    store = InMemorySourceStore()
    assert store.verified_citation_ratio() == 0.0

    c1 = store.add(make_card(1, doi="10.1000/one", url=None), role=AgentRole.RESEARCHER)
    c2 = store.add(make_card(2, doi="10.1000/two", url=None), role=AgentRole.RESEARCHER)
    c3 = store.add(make_card(3, doi="10.1000/three", url=None), role=AgentRole.RESEARCHER)

    store.set_verification(c1.id, VerificationStatus.VERIFIED, role=AgentRole.VALIDATOR)
    store.set_verification(c2.id, VerificationStatus.MISMATCH, role=AgentRole.VALIDATOR)

    assert store.verified_citation_ratio() == pytest.approx(1 / 3)
    assert store.verified_citation_ratio([c1.id, c2.id]) == pytest.approx(0.5)
    # Including an unknown/missing source_id counts against the denominator
    assert store.verified_citation_ratio([c1.id, sample_ulid(99)]) == pytest.approx(0.5)

    assert [c.id for c in store.list_sources(verification=VerificationStatus.VERIFIED)] == [c1.id]
    assert [c.id for c in store.list_sources(ids=[c3.id, c1.id])] == [c3.id, c1.id]

    with pytest.raises(SourceNotFoundError):
        store.require(sample_ulid(88))


def test_ingest_many_status_counts_and_validation_guards() -> None:
    """ingest_many deduplicates across a batch; status_counts and error guards hold."""
    store = InMemorySourceStore()
    c1 = make_card(1, doi="10.1000/batch.1", url=None)
    c1_dup = make_card(2, doi="https://doi.org/10.1000/BATCH.1", url=None)
    c2 = make_card(3, doi="10.1000/batch.2", url=None)

    results = store.ingest_many([c1, c1_dup, c2.model_dump()], role=AgentRole.RESEARCHER)
    assert len(results) == 3
    assert store.count == 2
    assert results[0].id == results[1].id

    store.set_verification(c1.id, VerificationStatus.VERIFIED, role=AgentRole.VALIDATOR)
    counts = store.status_counts()
    assert counts[VerificationStatus.VERIFIED] == 1
    assert counts[VerificationStatus.UNKNOWN] == 1
    assert counts[VerificationStatus.MISMATCH] == 0

    with pytest.raises(SourceVerificationError, match="blank"):
        store.set_verification(
            c1.id,
            VerificationStatus.VERIFIED,
            role=AgentRole.VALIDATOR,
            evidence="   ",
        )

    with pytest.raises(SourceNotFoundError):
        store.set_verification(
            sample_ulid(77),
            VerificationStatus.VERIFIED,
            role=AgentRole.VALIDATOR,
        )

    with pytest.raises(SourceStoreError, match="invalid SourceCard payload"):
        store.add({"id": sample_ulid(50), "title": "Incomplete"})

    with pytest.raises(SourceStoreError, match="expected SourceCard or mapping"):
        store.add(42)  # type: ignore[arg-type]
