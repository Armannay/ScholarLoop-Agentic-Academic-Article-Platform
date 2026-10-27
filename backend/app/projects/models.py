"""Relational model for projects.

A project is the user-facing container a run belongs to: a name, the brief the
article is written against, the policy overrides that apply to every run of the
project, a lifecycle status and timestamps. It is deliberately *not* a contract:
CONTRACTS v1 stays the wire format for run data, and ``docs/openapi`` records the
project record as a change request (see ``docs/arena-log/step-11/``).

Storage rules that matter:

* ``id`` is a 26-character ULID (the same id discipline as every other row), so a
  project id is stable, sortable and cannot be guessed;
* ``brief`` and ``policy_overrides`` are JSON documents whose shape is validated
  *before* insert by :mod:`app.projects.validation` - the row itself is dumb storage,
  exactly like the account rows;
* ``(owner_id, name)`` is unique per owner: re-reserving a name is a 409, never a
  silent second project;
* ``(owner_id, created_at, id)`` is indexed because that is the keyset the cursor
  pagination orders by.

The table lives on :class:`app.accounts.models.Base` (one metadata for the whole
application) so ``init_db`` and Alembic see the same schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.accounts.models import Base

__all__ = ["ProjectRow"]


class ProjectRow(Base):
    """One project owned by one user."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
        # The exact tuple the cursor walks: owner, newest first, ULID tie-break.
        Index("ix_projects_owner_created", "owner_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("users.id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(600), nullable=True)
    #: CONTRACTS v1 ``Brief`` as JSON (validated on the way in, re-validated on the way out).
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: Partial policy block, deep-merged over the defaults at run time.
    policy_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
