"""Relational models for the account store, usage accounting and audit trail.

Table names and columns follow the step contract exactly:

* ``users`` (id, email, password_hash)
* ``accounts`` (provider credentials, ciphertext only)
* ``agent_bindings`` (role -> ordered account ids + model caps)
* ``account_usage_daily`` (account_id, date, role, model, tokens_in, tokens_out,
  tokens_cached, est_cost)
* ``audit_log`` (actor, action, target, ts) - never secret-bearing; ``detail`` is
  redacted JSON written by the service layer

The wire contracts in :mod:`app.contracts` stay authoritative for shapes; these
rows persist those shapes. Mapping is explicit: rows are dumb storage, contracts
are validated on the way in and out.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AccountRow",
    "AccountUsageDailyRow",
    "AgentBindingRow",
    "AuditLogRow",
    "Base",
    "UserRow",
]


class Base(DeclarativeBase):
    """Declarative base shared by every account-store table."""


class UserRow(Base):
    """One account owner. Minimal auth: email + argon2 password hash + JWT."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountRow(Base):
    """One provider credential. ``key_ciphertext`` is an envelope blob (crypto.py)."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(64))
    key_ciphertext: Mapped[str] = mapped_column(Text)
    #: Display hint written at key-write time: ``****`` + last 4 chars. Not the key.
    key_mask: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    budget_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentBindingRow(Base):
    """Per-user, per-role provider configuration (mirrors ``AgentBinding``)."""

    __tablename__ = "agent_bindings"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_agent_bindings_user_role"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    #: Ordered candidate account ids as stored by the contract: ids only, never keys.
    account_ids: Mapped[list[str]] = mapped_column(JSON)
    strategy: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(120))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountUsageDailyRow(Base):
    """Daily token/cost roll-up per account. Append-or-add, never decremented."""

    __tablename__ = "account_usage_daily"

    account_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    model: Mapped[str] = mapped_column(String(120), primary_key=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    est_cost: Mapped[float] = mapped_column(Float, default=0.0)


class AuditLogRow(Base):
    """Append-only trail of every mutation. ``detail`` is redacted JSON, never raw."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
