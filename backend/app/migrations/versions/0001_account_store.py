"""Initial account store: users, accounts, agent_bindings, usage, audit.

Revision ID: 0001_account_store
Revises:
Create Date: 2026-10-18

Schema source of truth is ``app.accounts.models.Base``; this revision matches it
table-for-table (SQLite and Postgres both covered - batch mode is enabled for
SQLite in env.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_account_store"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("owner_id", sa.String(length=26), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("key_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_mask", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("budget_tokens", sa.Integer(), nullable=True),
        sa.Column("budget_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounts_owner_id", "accounts", ["owner_id"], unique=False)

    op.create_table(
        "agent_bindings",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("user_id", sa.String(length=26), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("account_ids", sa.JSON(), nullable=False),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "role", name="uq_agent_bindings_user_role"),
    )
    op.create_index("ix_agent_bindings_user_id", "agent_bindings", ["user_id"], unique=False)

    op.create_table(
        "account_usage_daily",
        sa.Column("account_id", sa.String(length=26), primary_key=True),
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("role", sa.String(length=16), primary_key=True),
        sa.Column("model", sa.String(length=120), primary_key=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("tokens_cached", sa.Integer(), nullable=False),
        sa.Column("est_cost", sa.Float(), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor"], unique=False)
    op.create_index("ix_audit_log_target", "audit_log", ["target"], unique=False)
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("account_usage_daily")
    op.drop_index("ix_agent_bindings_user_id", table_name="agent_bindings")
    op.drop_table("agent_bindings")
    op.drop_index("ix_accounts_owner_id", table_name="accounts")
    op.drop_table("accounts")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
