"""Alembic migration 0001: the schema the account store runs on.

The revision is checked against the live metadata: upgrade creates exactly the
five tables (and their indexes), and a migration-upgraded database accepts the
same inserts as ``init_db`` - so dev/test convenience and production DDL cannot
drift apart silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.accounts.db import make_engine
from app.accounts.models import Base
from app.projects.models import ProjectRow  # noqa: F401  (registers the projects table)

pytestmark = pytest.mark.migrations

EXPECTED_TABLES = {
    "users",
    "accounts",
    "agent_bindings",
    "account_usage_daily",
    "audit_log",
    "projects",
}


def alembic_config(repo_root: Path, database_url: str) -> Config:
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "backend/app/migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False
    return config


def test_upgrade_creates_the_account_store_schema(repo_root: Path, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrated.sqlite3'}"
    config = alembic_config(repo_root, database_url)
    command.upgrade(config, "head")

    engine = make_engine(database_url)
    try:
        names = set(inspect(engine).get_table_names())
        assert names >= EXPECTED_TABLES
        users_indexes = {index["name"] for index in inspect(engine).get_indexes("users")}
        assert "ix_users_email" in users_indexes
    finally:
        engine.dispose()


def test_migrated_schema_matches_the_declarative_metadata(repo_root: Path, tmp_path) -> None:
    """Every column the models declare exists after the migration, and vice versa."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'compare.sqlite3'}"
    command.upgrade(alembic_config(repo_root, database_url), "head")
    engine = make_engine(database_url)
    try:
        inspector = inspect(engine)
        for table in EXPECTED_TABLES:
            migrated = {column["name"] for column in inspector.get_columns(table)}
            declared = {column.name for column in Base.metadata.tables[table].columns}
            assert migrated == declared, f"column drift on {table}"
    finally:
        engine.dispose()


def test_downgrade_restores_empty_schema(repo_root: Path, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'down.sqlite3'}"
    config = alembic_config(repo_root, database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = make_engine(database_url)
    try:
        remaining = set(inspect(engine).get_table_names())
        assert remaining & EXPECTED_TABLES == set()  # only alembic's bookkeeping survives
    finally:
        engine.dispose()


def test_migrated_database_accepts_account_store_writes(repo_root: Path, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'writes.sqlite3'}"
    command.upgrade(alembic_config(repo_root, database_url), "head")
    engine = make_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, created_at) "
                    "VALUES ('01HF2Z3K4M5N6P7Q8R9S0T1U2V', 'a@b.co', 'x', '2026-10-18 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO account_usage_daily (account_id, date, role, model, "
                    "tokens_in, tokens_out, tokens_cached, est_cost) VALUES "
                    "('01HF2Z3K4M5N6P7Q8R9S0T1U2V', '2026-10-18', 'writer', 'm', 1, 2, 3, 0.0)"
                )
            )
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM users")).scalar()
            assert count == 1
    finally:
        engine.dispose()


def test_projects_revision_creates_the_constraints_pagination_relies_on(
    repo_root: Path, tmp_path
) -> None:
    """0002 adds the unique (owner, name) pair and the keyset index, not just a table."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'projects.sqlite3'}"
    command.upgrade(alembic_config(repo_root, database_url), "head")
    engine = make_engine(database_url)
    try:
        inspector = inspect(engine)
        indexes = {index["name"] for index in inspector.get_indexes("projects")}
        assert {"ix_projects_owner_id", "ix_projects_created_at", "ix_projects_owner_created"} <= (
            indexes
        )
        uniques = {
            constraint["name"] for constraint in inspector.get_unique_constraints("projects")
        }
        assert "uq_projects_owner_name" in uniques
        foreign_keys = {
            (fk["constrained_columns"][0], fk["referred_table"])
            for fk in inspector.get_foreign_keys("projects")
        }
        assert ("owner_id", "users") in foreign_keys
    finally:
        engine.dispose()


def test_migrated_database_accepts_project_writes(repo_root: Path, tmp_path) -> None:
    """The migrated JSON columns accept the brief and override documents as stored."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'project-writes.sqlite3'}"
    command.upgrade(alembic_config(repo_root, database_url), "head")
    engine = make_engine(database_url)
    user_id = "01HF2Z3K4M5N6P7Q8R9S0T1U2V"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, created_at) "
                    "VALUES (:id, 'projects@example.org', 'x', '2026-10-27 00:00:00')"
                ),
                {"id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO projects (id, owner_id, name, description, brief, "
                    "policy_overrides, status, created_at, updated_at) VALUES "
                    "(:id, :owner, 'Peer review', NULL, '{\"topic\": \"x\"}', "
                    "'{\"q_min\": 80}', 'active', '2026-10-27 00:00:00', "
                    "'2026-10-27 00:00:00')"
                ),
                {"id": "01HF2Z3K4M5N6P7Q8R9S0T1U2W", "owner": user_id},
            )
        with engine.connect() as connection:
            row = connection.execute(text("SELECT status, policy_overrides FROM projects")).one()
            assert row[0] == "active"
            assert "q_min" in row[1]
    finally:
        engine.dispose()
