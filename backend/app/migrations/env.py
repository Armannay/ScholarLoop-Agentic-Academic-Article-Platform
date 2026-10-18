"""Alembic environment for the account-store schema.

Runs against ``SCHOLARLOOP_DATABASE_URL`` (falling back to the settings default),
with ``app.accounts.models.Base`` as the target metadata. The initial revision is
written by hand to match ``Base`` exactly; ``alembic revision --autogenerate`` can
propose later changes from that baseline.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `app` importable when alembic runs from the repo root (alembic.ini lives
# there and only sets prepend_sys_path, which some runners ignore).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.accounts.models import Base  # noqa: E402
from app.core.settings import Settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL precedence: an explicit environment variable wins; then a URL passed
# programmatically (tests, --sql runs); then the Settings default (.env aware).
# The ini ships with the same default as Settings, so "default" is recognised
# by value.
_DEFAULT_URL = "sqlite+pysqlite:///./scholarloop.sqlite3"
_env_url = os.environ.get("SCHOLARLOOP_DATABASE_URL", "").strip()
_config_url = (config.get_main_option("sqlalchemy.url") or "").strip()
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)
elif not _config_url or _config_url == _DEFAULT_URL:
    config.set_main_option("sqlalchemy.url", Settings().database_url)

target_metadata = Base.metadata
database_url = config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=database_url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=database_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
