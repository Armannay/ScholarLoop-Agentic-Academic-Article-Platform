"""Projects: one row per owner-scoped project (name, brief, overrides, status).

Revision ID: 0002_projects
Revises: 0001_account_store
Create Date: 2026-10-27

Schema source of truth is ``app.projects.models.ProjectRow``; this revision matches it
column-for-column (batch mode is enabled for SQLite in ``env.py``).

Three constraints carry behaviour, not just hygiene:

* ``id`` is a 26-character ULID primary key - the same id discipline as every other
  row, so a project id is stable, sortable and not guessable;
* ``uq_projects_owner_name`` makes re-reserving a name a 409 at the database level,
  not only in a service check that a concurrent request could slip past;
* ``ix_projects_owner_created`` is exactly the keyset ``(owner_id, created_at, id)``
  the cursor pagination orders by, so a page is an index range scan.

``brief`` and ``policy_overrides`` are JSON documents validated by the application
before insert (``Brief`` contract and ``app.core.policies`` respectively); the columns
are intentionally untyped JSON so a future contract revision does not need a migration
that only widens a shape.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_projects"
down_revision: str | None = "0001_account_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("owner_id", sa.String(length=26), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=600), nullable=True),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("policy_overrides", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"], unique=False)
    op.create_index("ix_projects_created_at", "projects", ["created_at"], unique=False)
    op.create_index(
        "ix_projects_owner_created",
        "projects",
        ["owner_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_projects_owner_created", table_name="projects")
    op.drop_index("ix_projects_created_at", table_name="projects")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
