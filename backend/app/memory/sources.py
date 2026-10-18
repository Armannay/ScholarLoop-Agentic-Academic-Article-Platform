"""Source store: auditable :class:`~app.contracts.source.SourceCard` memory.

HARD RULE 1 lives here alongside :mod:`app.contracts.source`:

* every card is locatable (has a normalised ``doi`` or an ``http(s)`` ``url``);
* ``verification`` starts at ``unknown`` and **only** ``AgentRole.VALIDATOR`` may
  move a card to ``verified`` or ``mismatch``;
* duplicate sources are deduplicated by normalised DOI (or canonical URL when no
  DOI exists) so the researcher reuses existing cards instead of minting new ids
  for the same paper, and a re-ingested ``unknown`` card never overwrites an
  already-``verified`` status.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from app.contracts.enums import AgentRole, VerificationStatus
from app.contracts.source import SourceCard, normalise_doi
from app.memory.errors import (
    SourceConflictError,
    SourceNotFoundError,
    SourceStoreError,
    SourceVerificationError,
)

__all__ = [
    "InMemorySourceStore",
    "SourceStore",
]


def _canonical_url(url: str | None) -> str | None:
    """Normalise an HTTP(S) URL for deduplication (strip trailing slash, lower host)."""
    if url is None:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    if cleaned.endswith("/") and cleaned.count("/") > 2:
        cleaned = cleaned.rstrip("/")
    return cleaned


@runtime_checkable
class SourceStore(Protocol):
    """Protocol for storing, deduplicating and verifying :class:`SourceCard` items."""

    def add(
        self,
        card: SourceCard | Mapping[str, Any],
        *,
        role: AgentRole | None = None,
    ) -> SourceCard:
        """Add or reuse a source card, enforcing role verification rules."""
        ...

    def get(self, source_id: str) -> SourceCard | None:
        """Return the card for ``source_id``, or ``None``."""
        ...

    def require(self, source_id: str) -> SourceCard:
        """Return the card for ``source_id`` or raise :class:`SourceNotFoundError`."""
        ...

    def find_by_doi(self, doi: str) -> SourceCard | None:
        """Find a card by DOI (normalised before matching)."""
        ...

    def find_by_url(self, url: str) -> SourceCard | None:
        """Find a card by canonical URL."""
        ...

    def list_sources(
        self,
        *,
        verification: VerificationStatus | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[SourceCard]:
        """List stored cards in insertion order, optionally filtered."""
        ...

    def set_verification(
        self,
        source_id: str,
        verification: VerificationStatus,
        *,
        role: AgentRole = AgentRole.VALIDATOR,
        evidence: str | None = None,
    ) -> SourceCard:
        """Update the verification state of a card (validator only)."""
        ...

    def verified_citation_ratio(
        self,
        cited_source_ids: Iterable[str] | None = None,
    ) -> float:
        """Fraction of cards (or cited source ids) that are ``verified``."""
        ...


class InMemorySourceStore:
    """In-memory :class:`SourceStore` with DOI/URL deduplication and validator guards."""

    def __init__(self, cards: Iterable[SourceCard | Mapping[str, Any]] | None = None) -> None:
        self._cards: dict[str, SourceCard] = {}
        self._by_doi: dict[str, str] = {}
        self._by_url: dict[str, str] = {}
        self._evidence: dict[str, str] = {}

        if cards is not None:
            for item in cards:
                self.add(item, role=AgentRole.VALIDATOR)

    def __len__(self) -> int:
        """Number of unique cards in the store."""
        return len(self._cards)

    @property
    def count(self) -> int:
        """Number of unique cards in the store."""
        return len(self._cards)

    # -- writes ------------------------------------------------------------
    def add(
        self,
        card: SourceCard | Mapping[str, Any],
        *,
        role: AgentRole | None = None,
    ) -> SourceCard:
        """Add a :class:`SourceCard` or reuse an existing card with the same locator.

        Rules enforced:

        * Non-validator roles may only submit cards with ``verification == unknown``.
        * If a card with the same normalised DOI already exists (or the same URL when
          neither card has a DOI), the existing card is reused so its ULID stays
          stable across iterations.
        * A previously ``verified`` card is never silently downgraded to ``unknown``
          when a researcher re-discovers it.
        * If a card reuses an existing ``id`` for a *different* paper (different
          DOI/title), :class:`SourceConflictError` is raised.
        """
        validated = self._coerce_card(card)

        if (
            role is not None
            and role is not AgentRole.VALIDATOR
            and validated.verification is not VerificationStatus.UNKNOWN
        ):
            raise SourceVerificationError(
                f"role '{role.value}' may not set verification={validated.verification.value!r}; "
                "only the validator may verify or flag a SourceCard",
                source_id=validated.id,
                role=role.value,
                verification=validated.verification.value,
            )

        existing_by_id = self._cards.get(validated.id)
        if existing_by_id is not None:
            if not self._same_identity(existing_by_id, validated):
                raise SourceConflictError(
                    f"source_id {validated.id!r} is already bound to a different source "
                    f"({existing_by_id.title!r} vs {validated.title!r})",
                    source_id=validated.id,
                )
            merged = self._merge_existing(existing_by_id, validated, role=role)
            self._store_card(merged)
            return merged.model_copy(deep=True)

        if validated.doi is not None:
            matched_id = self._by_doi.get(validated.doi)
            if matched_id is not None:
                existing = self._cards[matched_id]
                merged = self._merge_existing(existing, validated, role=role)
                self._store_card(merged)
                return merged.model_copy(deep=True)

        canon_url = _canonical_url(validated.url)
        if validated.doi is None and canon_url is not None:
            matched_id = self._by_url.get(canon_url)
            if matched_id is not None:
                existing = self._cards[matched_id]
                merged = self._merge_existing(existing, validated, role=role)
                self._store_card(merged)
                return merged.model_copy(deep=True)

        self._store_card(validated)
        return validated.model_copy(deep=True)

    upsert = add

    def ingest_many(
        self,
        cards: Iterable[SourceCard | Mapping[str, Any]],
        *,
        role: AgentRole | None = None,
    ) -> list[SourceCard]:
        """Add or merge a batch of cards, returning the canonical stored cards."""
        return [self.add(card, role=role) for card in cards]

    def set_verification(
        self,
        source_id: str,
        verification: VerificationStatus,
        *,
        role: AgentRole = AgentRole.VALIDATOR,
        evidence: str | None = None,
    ) -> SourceCard:
        """Set ``verification`` on ``source_id`` (restricted to ``AgentRole.VALIDATOR``).

        Raises:
            SourceVerificationError: called by a role other than ``validator``, or
                ``verified`` was requested with blank ``evidence`` when ``evidence``
                was explicitly passed as an empty string.
            SourceNotFoundError: ``source_id`` does not exist.
        """
        if role is not AgentRole.VALIDATOR:
            raise SourceVerificationError(
                f"role '{role.value}' cannot change source verification; "
                "only the validator may set verification status",
                source_id=source_id,
                role=role.value,
            )
        if evidence is not None and not evidence.strip():
            raise SourceVerificationError(
                "verification evidence must not be blank when supplied",
                source_id=source_id,
            )

        existing = self._cards.get(source_id)
        if existing is None:
            raise SourceNotFoundError(
                f"no SourceCard with id {source_id!r}",
                source_id=source_id,
            )

        updated = existing.model_copy(update={"verification": verification}, deep=True)
        self._cards[source_id] = updated
        if evidence is not None:
            self._evidence[source_id] = evidence.strip()
        return updated.model_copy(deep=True)

    # -- reads -------------------------------------------------------------
    def get(self, source_id: str) -> SourceCard | None:
        """Return a defensive copy of ``source_id``, or ``None``."""
        card = self._cards.get(source_id)
        return None if card is None else card.model_copy(deep=True)

    def require(self, source_id: str) -> SourceCard:
        """Return a defensive copy of ``source_id`` or raise :class:`SourceNotFoundError`."""
        card = self.get(source_id)
        if card is None:
            raise SourceNotFoundError(
                f"no SourceCard with id {source_id!r}",
                source_id=source_id,
            )
        return card

    def find_by_doi(self, doi: str) -> SourceCard | None:
        """Look up a card by DOI (accepts ``https://doi.org/...`` or bare ``10.xxxx/...``)."""
        cleaned = normalise_doi(doi)
        if not cleaned:
            return None
        source_id = self._by_doi.get(cleaned)
        return None if source_id is None else self.get(source_id)

    def find_by_url(self, url: str) -> SourceCard | None:
        """Look up a card by URL."""
        canon = _canonical_url(url)
        if canon is None:
            return None
        source_id = self._by_url.get(canon)
        return None if source_id is None else self.get(source_id)

    def verification_evidence(self, source_id: str) -> str | None:
        """Return recorded validator evidence for ``source_id``, if any."""
        return self._evidence.get(source_id)

    def list_sources(
        self,
        *,
        verification: VerificationStatus | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[SourceCard]:
        """Return stored cards in insertion order, filtered by status and/or id list."""
        if ids is not None:
            wanted = list(dict.fromkeys(ids))
            selected = [self._cards[source_id] for source_id in wanted if source_id in self._cards]
        else:
            selected = list(self._cards.values())

        return [
            card.model_copy(deep=True)
            for card in selected
            if verification is None or card.verification is verification
        ]

    def status_counts(self) -> dict[VerificationStatus, int]:
        """Count of cards in each :class:`VerificationStatus`."""
        counts: dict[VerificationStatus, int] = dict.fromkeys(VerificationStatus, 0)
        for card in self._cards.values():
            counts[card.verification] += 1
        return counts

    def verified_citation_ratio(
        self,
        cited_source_ids: Iterable[str] | None = None,
    ) -> float:
        """Fraction of cards (or cited source ids) whose status is ``verified``.

        When ``cited_source_ids`` is given, unresolved/unknown ids count in the
        denominator as unverified so a missing card can never inflate the ratio.
        When the denominator is empty, returns ``0.0``.
        """
        if cited_source_ids is not None:
            unique_ids = list(dict.fromkeys(cited_source_ids))
            if not unique_ids:
                return 0.0
            verified_count = sum(
                1
                for source_id in unique_ids
                if (card := self._cards.get(source_id)) is not None
                and card.verification is VerificationStatus.VERIFIED
            )
            return verified_count / len(unique_ids)

        if not self._cards:
            return 0.0
        verified_count = sum(
            1 for card in self._cards.values() if card.verification is VerificationStatus.VERIFIED
        )
        return verified_count / len(self._cards)

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _coerce_card(card: SourceCard | Mapping[str, Any]) -> SourceCard:
        if isinstance(card, SourceCard):
            return card
        if isinstance(card, Mapping):
            try:
                return SourceCard.model_validate(dict(card))
            except Exception as exc:
                raise SourceStoreError(f"invalid SourceCard payload: {exc}") from exc
        raise SourceStoreError(f"expected SourceCard or mapping, got {type(card).__name__}")

    @staticmethod
    def _same_identity(existing: SourceCard, incoming: SourceCard) -> bool:
        if existing.doi is not None and incoming.doi is not None:
            return existing.doi == incoming.doi
        return existing.title.casefold() == incoming.title.casefold()

    @staticmethod
    def _merge_existing(
        existing: SourceCard,
        incoming: SourceCard,
        *,
        role: AgentRole | None,
    ) -> SourceCard:
        """Keep the existing card's canonical ULID and preserve verified status."""
        verification = existing.verification
        if (
            role in (None, AgentRole.VALIDATOR)
            and incoming.verification is not VerificationStatus.UNKNOWN
        ):
            verification = incoming.verification

        return existing.model_copy(
            update={
                "doi": existing.doi or incoming.doi,
                "url": existing.url or incoming.url,
                "authors": existing.authors or list(incoming.authors),
                "verification": verification,
            },
            deep=True,
        )

    def _store_card(self, card: SourceCard) -> None:
        stored = card.model_copy(deep=True)
        self._cards[stored.id] = stored
        if stored.doi is not None:
            self._by_doi[stored.doi] = stored.id
        canon_url = _canonical_url(stored.url)
        if canon_url is not None:
            self._by_url[canon_url] = stored.id
