"""Shared pytest fixtures.

HARD RULE 5 lives here: ``mock_provider`` is the only provider any test may use, and
``settings`` points at the real config files in the repo root so tests exercise the
same policies and rubric that production loads.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.crypto import EnvelopeEncryptor, MasterKey
from app.accounts.db import create_session_factory, init_db, make_engine
from app.accounts.service import AccountService, AuditService, BindingService, UserService
from app.api.main import create_app
from app.contracts.llm import LLMRequest
from app.core.ids import UlidFactory
from app.core.policies import load_policies
from app.core.rubric import load_rubric
from app.core.settings import Settings
from app.providers.mock import MockProvider, Script
from tests.factories import FIXED_TS_MS, make_request

#: Repository root (this file is backend/tests/conftest.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Path to the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    """Path to config/."""
    return repo_root / "config"


@pytest.fixture
def settings(repo_root: Path) -> Settings:
    """Settings rooted at the repository, with no secret key (live providers off)."""
    return Settings(root_dir=repo_root, env="test", secret_key=None)


@pytest.fixture
def policies(settings: Settings):
    """Policies loaded from the shipped config."""
    return load_policies(settings.policies_file)


@pytest.fixture
def rubric(settings: Settings):
    """Rubric loaded from the shipped config."""
    return load_rubric(settings.rubric_file)


@pytest.fixture
def ulid_factory() -> UlidFactory:
    """Deterministic ULID factory: same ids in every run, no global state."""
    return UlidFactory(timestamp_ms=FIXED_TS_MS)


@pytest.fixture
def mock_provider() -> Iterator[MockProvider]:
    """A scripted MockProvider. Never makes a network call."""
    yield MockProvider(Script())


@pytest.fixture
def llm_request() -> LLMRequest:
    """A minimal valid request."""
    return make_request()


# --------------------------------------------------------------------------- account store
# The fixtures below cover this step's account store: master key, database,
# sessions, users and an authenticated API client. They never touch the network.


@pytest.fixture
def master_key_raw() -> str:
    """A synthetic master key: 64 hex chars, never a real deployment key."""
    return "ab" * 32


@pytest.fixture
def master_key(master_key_raw: str) -> MasterKey:
    return MasterKey.from_string(master_key_raw)


@pytest.fixture
def encryptor(master_key: MasterKey) -> EnvelopeEncryptor:
    return EnvelopeEncryptor(master_key)


@pytest.fixture
def app_master_key_env(monkeypatch: pytest.MonkeyPatch, master_key_raw: str) -> str:
    """APP_MASTER_KEY in the environment for the duration of a test."""
    monkeypatch.setenv("APP_MASTER_KEY", master_key_raw)
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    monkeypatch.delenv("SCHOLARLOOP_JWT_SECRET", raising=False)
    return master_key_raw


@pytest.fixture
def db_url(tmp_path) -> str:
    """A throwaway SQLite database per test."""
    return f"sqlite+pysqlite:///{tmp_path / 'accounts-test.sqlite3'}"


@pytest.fixture
def engine(db_url: str):
    eng = make_engine(db_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    sess = session_factory()
    yield sess
    sess.close()


@pytest.fixture
def audit(session: Session) -> AuditService:
    return AuditService(session)


@pytest.fixture
def user_service(session: Session) -> UserService:
    return UserService(session)


@pytest.fixture
def account_service(session: Session, encryptor: EnvelopeEncryptor) -> AccountService:
    return AccountService(session, encryptor)


@pytest.fixture
def binding_service(session: Session) -> BindingService:
    return BindingService(session)


@pytest.fixture
def user_row(user_service: UserService, session: Session):
    row = user_service.register("owner@example.org", "correct-horse-battery")
    session.commit()
    return row


@pytest.fixture
def api_settings(repo_root: Path, db_url: str) -> Settings:
    return Settings(root_dir=repo_root, env="test", database_url=db_url, secret_key=None)


@pytest.fixture
def client(api_settings: Settings, app_master_key_env: str) -> Iterator[TestClient]:
    """Authenticated-boot TestClient on a throwaway database, master key present."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client  # type: ignore[misc]


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """Register a fresh owner and return bearer headers for them."""
    response = client.post(
        "/api/auth/register",
        json={"email": "owner@example.org", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}
