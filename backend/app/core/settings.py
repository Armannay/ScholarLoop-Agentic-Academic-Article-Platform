"""Runtime settings from the environment (see .env.example).

Nothing here is a secret *value*: ``secret_key`` arrives from the environment or the
deployment's secret manager and is held as a ``SecretStr``, so it prints as
``**********`` in logs and tracebacks (HARD RULE 2).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "reset_settings_cache"]

VALID_ENVS = ("dev", "test", "docker", "staging", "prod")


class Settings(BaseSettings):
    """Environment-driven configuration. Unknown variables are ignored on purpose."""

    model_config = SettingsConfigDict(
        env_prefix="SCHOLARLOOP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = "dev"
    log_level: str = "INFO"

    #: SQLAlchemy URL. SQLite in dev, Postgres in production - the code never
    #: branches on the dialect.
    database_url: str = "sqlite+pysqlite:///./scholarloop.sqlite3"

    #: Key material for the encrypted account store. Empty => live providers disabled.
    secret_key: SecretStr | None = None

    #: Paths are resolved against ``root_dir`` so behaviour does not depend on CWD.
    root_dir: Path = Path()
    policies_path: Path = Path("config/policies.yaml")
    rubric_path: Path = Path("config/rubric.yaml")
    rules_dir: Path = Path("rules")
    project_overrides_path: Path | None = None

    # 0.0.0.0 is deliberate: the API must be reachable from inside a container and
    # from the sandbox preview proxy. Bind loopback explicitly in production.
    api_host: str = "0.0.0.0"  # noqa: S104
    api_port: int = Field(default=8000, ge=1, le=65_535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    project_name: str | None = Field(
        default=None,
        description="Selects a `projects:` block in the policy overrides, when present.",
    )

    @field_validator("env")
    @classmethod
    def _known_env(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in VALID_ENVS:
            raise ValueError(f"env must be one of {VALID_ENVS}, got {value!r}")
        return lowered

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.strip().upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @field_validator("secret_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        """``SCHOLARLOOP_SECRET_KEY=`` in a .env file means 'not configured', not ''."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept ``a,b,c`` in the environment (no JSON quoting required)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("project_overrides_path", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def resolve(self, path: Path) -> Path:
        """Resolve a configured path against ``root_dir``."""
        return path if path.is_absolute() else (self.root_dir / path).resolve()

    @property
    def policies_file(self) -> Path:
        """Absolute path to config/policies.yaml."""
        return self.resolve(self.policies_path)

    @property
    def rubric_file(self) -> Path:
        """Absolute path to config/rubric.yaml."""
        return self.resolve(self.rubric_path)

    @property
    def rules_path(self) -> Path:
        """Absolute path to rules/."""
        return self.resolve(self.rules_dir)

    @property
    def live_providers_enabled(self) -> bool:
        """True only when a secret key is configured, i.e. the account store is usable."""
        return self.secret_key is not None and bool(self.secret_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton (env is read once per process)."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings - used by tests and by config reload endpoints."""
    get_settings.cache_clear()
