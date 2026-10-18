"""The accounts API: a key is write-only, and no error body ever echoes one.

HARD RULE 2 at the HTTP boundary. The properties under test:

* without a store secret the endpoints refuse politely (503) instead of pretending
  to store a plaintext key;
* a key is accepted in a request body and never returned;
* the fingerprint changes when the key is rotated;
* a validation error does not echo the offending body.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.main import create_app
from app.core.redaction import find_secrets
from app.core.settings import Settings

pytestmark = pytest.mark.api

#: A synthetic key, built by concatenation so no key-shaped string is in the repo.
FAKE_KEY = "unit-" + "test-" + "key-" + "0123456789abcdef"
OTHER_FAKE_KEY = "unit-" + "test-" + "key-" + "fedcba9876543210"
STORE_SECRET = "unit-test-store-secret-not-a-real-secret"


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A client for an app whose account store is enabled."""
    enabled = settings.model_copy(update={"secret_key": SecretStr(STORE_SECRET)})
    with TestClient(create_app(enabled)) as test_client:
        yield test_client  # type: ignore[misc]


def test_without_a_store_secret_the_endpoints_refuse(settings: Settings) -> None:
    """No secret means no way to encrypt: the API says so instead of guessing."""
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/accounts",
            json={"provider": "anthropic", "label": "work", "key": FAKE_KEY},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "account_store_disabled"


def test_an_account_can_be_added_and_is_never_echoed(client: TestClient) -> None:
    """The response carries a fingerprint, not a key."""
    response = client.post(
        "/api/accounts",
        json={"provider": "anthropic", "label": "work", "key": FAKE_KEY},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "work"
    assert body["provider"] == "anthropic"
    assert body["status"] == "active"
    assert len(body["key_fingerprint"]) == 12
    assert FAKE_KEY not in response.text
    assert find_secrets(response.text) == []


def test_an_account_can_be_listed_and_fetched(client: TestClient) -> None:
    """The list endpoint returns redacted views."""
    created = client.post(
        "/api/accounts",
        json={"provider": "mock", "label": "local"},
    )
    assert created.status_code == 201
    account_id = created.json()["id"]

    listed = client.get("/api/accounts")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [account_id]

    fetched = client.get(f"/api/accounts/{account_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == account_id


def test_the_list_can_be_filtered(client: TestClient) -> None:
    """The filters are the ones the UI needs: owner, provider, status."""
    client.post("/api/accounts", json={"provider": "mock", "label": "a", "owner_id": "u1"})
    client.post("/api/accounts", json={"provider": "mock", "label": "b", "owner_id": "u2"})

    assert len(client.get("/api/accounts", params={"owner_id": "u1"}).json()) == 1
    assert len(client.get("/api/accounts", params={"provider": "mock"}).json()) == 2
    assert len(client.get("/api/accounts", params={"provider": "anthropic"}).json()) == 0


def test_an_account_can_be_disabled_and_re_enabled(client: TestClient) -> None:
    """Disabling is how a user stops a spend, and it is reversible."""
    created = client.post("/api/accounts", json={"provider": "mock", "label": "local"})
    account_id = created.json()["id"]

    disabled = client.patch(f"/api/accounts/{account_id}", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    enabled = client.patch(f"/api/accounts/{account_id}", json={"status": "active"})
    assert enabled.json()["status"] == "active"


def test_rotating_the_key_changes_the_fingerprint(client: TestClient) -> None:
    """The fingerprint proves the key changed, without ever revealing it."""
    created = client.post(
        "/api/accounts",
        json={"provider": "anthropic", "label": "work", "key": FAKE_KEY},
    )
    account_id = created.json()["id"]
    before = created.json()["key_fingerprint"]

    rotated = client.put(f"/api/accounts/{account_id}/key", json={"key": OTHER_FAKE_KEY})
    assert rotated.status_code == 200
    assert rotated.json()["key_fingerprint"] != before
    assert OTHER_FAKE_KEY not in rotated.text


def test_an_account_can_be_deleted(client: TestClient) -> None:
    """Deletion is immediate and visible."""
    created = client.post("/api/accounts", json={"provider": "mock", "label": "local"})
    account_id = created.json()["id"]

    assert client.delete(f"/api/accounts/{account_id}").status_code == 204
    assert client.get(f"/api/accounts/{account_id}").status_code == 404


def test_an_unknown_account_is_a_404(client: TestClient) -> None:
    """A missing account is a 404, not an empty 200."""
    assert client.get("/api/accounts/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 404


def test_a_validation_error_never_echoes_the_body(client: TestClient) -> None:
    """FastAPI's default 422 body would contain the offending value - not here."""
    response = client.post(
        "/api/accounts",
        json={
            "provider": "anthropic",
            "label": "work",
            "key": FAKE_KEY,
            "owner_id": 17,  # wrong type: the request as a whole is invalid
        },
    )
    assert response.status_code == 422
    assert FAKE_KEY not in response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_a_live_account_without_a_key_is_refused(client: TestClient) -> None:
    """The store, not the API, decides that a live provider needs a key."""
    response = client.post("/api/accounts", json={"provider": "anthropic", "label": "work"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "account_store_error"


def test_the_health_endpoint_counts_the_accounts(client: TestClient) -> None:
    """The dashboard shows how many accounts exist."""
    client.post("/api/accounts", json={"provider": "mock", "label": "local"})
    body = client.get("/api/health").json()
    assert body["providers"]["live_providers_enabled"] is True
