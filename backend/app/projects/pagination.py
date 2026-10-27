"""Opaque keyset cursors for ``GET /api/projects``.

Offset pagination ("skip 40") silently duplicates and drops rows when a project is
created between two pages. Keyset pagination does not: the cursor names the exact
``(created_at, id)`` the previous page ended on, and the next query asks for rows
strictly older than that tuple - ``id`` breaks ties when two projects share a
timestamp.

The cursor is base64url(JSON) rather than a bare timestamp so a client cannot be
tempted to construct one by hand, and so the format can grow (a sort key, a filter
fingerprint) without a client change. It is not a security boundary: the query is
always scoped to the caller's ``owner_id``, so a forged cursor can only move a user
around their own list. An unreadable cursor is a 422 with ``loc=cursor``, never a 500.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.ids import is_ulid

__all__ = ["CURSOR_MAX_LENGTH", "ProjectCursor", "decode_cursor", "encode_cursor"]

#: Longest cursor accepted. A real one is ~90 chars; this bounds the parse work.
CURSOR_MAX_LENGTH = 512


@dataclass(frozen=True)
class ProjectCursor:
    """The sort key of the last row of a page."""

    created_at: datetime
    id: str


def encode_cursor(created_at: datetime, project_id: str) -> str:
    """Encode one row's sort key. Aware UTC in, opaque ASCII out."""
    moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    payload = json.dumps(
        {"t": moment.astimezone(UTC).isoformat(), "id": project_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> ProjectCursor:
    """Decode and validate a cursor.

    Raises:
        ValueError: empty, oversized, not base64url JSON, or not ``(ISO-8601 UTC,
            ULID)``. Callers map this to a 422 naming the ``cursor`` parameter.
    """
    text = value.strip()
    if not text:
        raise ValueError("cursor is empty")
    if len(text) > CURSOR_MAX_LENGTH:
        raise ValueError(f"cursor is longer than {CURSOR_MAX_LENGTH} characters")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cursor is not valid base64url JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("cursor payload must be a JSON object")
    moment = data.get("t")
    project_id = data.get("id")
    if not isinstance(moment, str) or not isinstance(project_id, str):
        raise ValueError("cursor needs a string 't' and a string 'id'")
    if not is_ulid(project_id):
        raise ValueError("cursor 'id' is not a ULID")
    try:
        parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"cursor 't' is not ISO-8601 ({exc})") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return ProjectCursor(created_at=parsed.astimezone(UTC), id=project_id)
