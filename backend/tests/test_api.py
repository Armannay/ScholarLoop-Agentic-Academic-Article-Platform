"""API surface tests: health, effective config, contract schemas, failure modes.

Two properties matter most here: the app must boot even with a broken config file
(and say so), and no endpoint may ever return key material.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import CONTRACT_VERSION
from app.api.main import create_app
from app.contracts.registry import CONTRACT_NAMES
from app.core.redaction import find_secrets
from app.core.settings import Settings
from tests.factories import SAMPLE_BRIEF, sample_ulid

pytestmark = pytest.mark.api


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """Client for the healthy app (shipped config, MockProvider only)."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client  # type: ignore[misc]


# --------------------------------------------------------------------------- health
def test_health_reports_loaded_config(client: TestClient) -> None:
    """A healthy boot says exactly what is loaded and which providers exist."""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["policies_loaded"] is True
    assert body["rubric_loaded"] is True
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["providers"]["mock"] is True
    assert body["providers"]["live_adapters_implemented"] == []
    assert body["config_errors"] == {}


def test_health_is_secret_free(client: TestClient) -> None:
    """No key-shaped string may appear in a response body (HARD RULE 2)."""
    for path in ("/api/health", "/api/config", "/api/version", "/api/contracts", "/"):
        assert find_secrets(client.get(path).text) == [], f"secret-shaped text in {path}"


def test_every_response_carries_trace_headers(client: TestClient) -> None:
    """A log line can always be traced back to a request."""
    response = client.get("/api/health")
    assert response.headers["x-contract-version"] == str(CONTRACT_VERSION)
    assert len(response.headers["x-request-id"]) == 26


def test_request_id_can_be_supplied_by_the_caller(client: TestClient) -> None:
    """An upstream proxy's request id is preserved instead of replaced."""
    response = client.get("/api/health", headers={"x-request-id": "caller-supplied-id"})
    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_version_endpoint(client: TestClient) -> None:
    """Version identifiers in one call, for the UI footer."""
    body = client.get("/api/version").json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["provider_default"] == "mock"
    assert body["env"] == "test"


def test_root_landing_payload(client: TestClient) -> None:
    """A bare visit gets a useful pointer, not a 404."""
    body = client.get("/").json()
    assert body["service"] == "scholarloop"
    assert body["health"] == "/api/health"


# --------------------------------------------------------------------------- config
def test_config_returns_effective_policies_and_rubric(client: TestClient) -> None:
    """The UI needs the *merged* numbers, not the file's."""
    body = client.get("/api/config").json()
    assert body["policies"]["run_token_cap"] == 800_000
    assert body["policies"]["q_min"] == 70
    assert body["policies"]["model_tiers"]["reviewer"] == "strong"
    assert len(body["rubric"]["gates"]) == 8
    assert body["rubric"]["weights_total"] == pytest.approx(100.0)
    assert "research" in body["rubric"]["article_types"]


def test_config_reports_where_it_read_from(client: TestClient, settings: Settings) -> None:
    """Effective config must name its sources, so a merge surprise is traceable."""
    body = client.get("/api/config").json()
    assert body["policies_path"] == str(settings.policies_file)
    assert body["overrides_path"] is None
    assert body["project"] is None


def test_config_honours_project_overrides(repo_root: Path) -> None:
    """A project override changes the numbers the API reports."""
    settings = Settings(
        root_dir=repo_root,
        env="test",
        project_overrides_path=Path("config/projects.example.yaml"),
        project_name="thesis-chapter",
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/api/config").json()
        assert body["project"] == "thesis-chapter"
        assert body["policies"]["q_min"] == 80
        assert body["policies"]["plateau"]["min_gain"] == 2.0
        assert body["overrides_path"] is not None


def test_broken_config_degrades_instead_of_crashing(tmp_path: Path, repo_root: Path) -> None:
    """One bad key must not stop the process from serving a diagnosis."""
    bad_policies = tmp_path / "policies.yaml"
    bad_policies.write_text("schema_version: 1\npolicies:\n  q_minn: 60\n", encoding="utf-8")

    settings = Settings(root_dir=repo_root, env="test", policies_path=bad_policies)
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert health.json()["policies_loaded"] is False
        assert "q_minn" in health.json()["config_errors"]["policies"]

        config = client.get("/api/config")
        assert config.status_code == 503
        assert config.json()["detail"]["code"] == "config_error"


# ------------------------------------------------------------------------ contracts
def test_contract_registry_endpoint(client: TestClient) -> None:
    """Clients discover contracts instead of hard-coding names."""
    body = client.get("/api/contracts").json()
    assert body["names"] == list(CONTRACT_NAMES)
    assert body["schema_url_template"] == "/api/contracts/{name}/schema"


@pytest.mark.parametrize("name", ["Brief", "Plan", "AgentResult", "LLMRequest"])
def test_contract_schema_endpoint(client: TestClient, name: str) -> None:
    """Every contract has a fetchable JSON Schema at the advertised URL."""
    response = client.get(f"/api/contracts/{name}/schema")
    assert response.status_code == 200
    schema = response.json()
    assert schema["title"] == name
    assert schema["additionalProperties"] is False


def test_unknown_contract_is_a_404_not_a_500(client: TestClient) -> None:
    """A bad name is a client error; the app must not leak a stack trace."""
    response = client.get("/api/contracts/Nope/schema")
    assert response.status_code == 404


def test_openapi_documents_every_route(client: TestClient) -> None:
    """The OpenAPI document is the contract with the rest of the world."""
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/health", "/api/config", "/api/contracts"} <= set(paths)


def test_brief_payload_validates_through_the_api(client: TestClient) -> None:
    """Sanity check that the contracts are usable from the outside."""
    from app.contracts import Brief

    brief = Brief.model_validate(SAMPLE_BRIEF)
    assert brief.target_words == 4000

    broken = dict(SAMPLE_BRIEF)
    broken.pop("citation_style")
    with pytest.raises(Exception):  # noqa: B017 - pydantic's error type is the point
        Brief.model_validate(broken)


def test_cors_allows_the_vite_dev_server(client: TestClient) -> None:
    """The frontend talks to /api through the dev server proxy, but direct calls work too."""
    response = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_the_account_surface_refuses_to_work_without_a_store_secret(client: TestClient) -> None:
    """The account surface exists, but it needs a secret to encrypt a key with.

    Without ``SCHOLARLOOP_SECRET_KEY`` there is no way to store a key safely, so
    the endpoints say so (503) instead of accepting a plaintext one.
    """
    response = client.get("/api/accounts")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "account_store_disabled"
    assert sample_ulid(3)  # ids are available for the payloads that endpoint takes
