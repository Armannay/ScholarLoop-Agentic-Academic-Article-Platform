"""Shared pytest fixtures.

HARD RULE 5 lives here: ``mock_provider`` is the only provider any test may use, and
``settings`` points at the real config files in the repo root so tests exercise the
same policies and rubric that production loads.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

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
