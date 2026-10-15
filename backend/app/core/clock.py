"""Clock helpers. Everything in ScholarLoop is UTC and timezone-aware."""

from __future__ import annotations

from datetime import UTC, datetime

UTC = UTC


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


def utc_from_epoch(seconds: float) -> datetime:
    """Convert epoch seconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(seconds, tz=UTC)


def to_utc(value: datetime) -> datetime:
    """Normalise any datetime to UTC.

    Naive datetimes are interpreted as UTC rather than rejected, so legacy data
    cannot smuggle in a local-time offset. ``fromisoformat`` accepts the ``Z``
    suffix on 3.11+, so callers can parse before validating.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
