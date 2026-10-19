"""HTTP surface: auth, account CRUD, test endpoint, bindings, delete protection.

Allowed and denied paths both get pinned: every mutation requires a bearer token,
every cross-user probe answers 404, and no response ever carries key material.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_accounts_crypto import api_key

pytestmark = pytest.mark.api

PASS = "correct-horse-battery"
MODEL = "claude-sonnet-4-5"


def register(client: TestClient, email: str = "owner@example.org") -> dict[str, str]:
    response = client.post("/api/auth/register", json={"email": email, "password": PASS})
    assert response.status_code == 201, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}


def create_account(
    client: TestClient,
    headers: dict[str, str],
    *,
    provider: str = "anthropic",
    label: str = "work",
    key: str | None = None,
    base_url: str | None = None,
) -> dict:
    payload: dict = {"provider": provider, "label": label, "api_key": key or api_key()}
    if base_url is not None:
        payload["base_url"] = base_url
    response = client.post("/api/accounts", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def bind(
    client: TestClient,
    headers: dict[str, str],
    role: str,
    account_ids: list[str],
    *,
    strategy: str = "dedicated",
    enabled: bool = True,
) -> dict:
    response = client.put(
        f"/api/agents/{role}/binding",
        json={
            "account_ids": account_ids,
            "strategy": strategy,
            "model": MODEL,
            "max_input_tokens": 24_000,
            "max_output_tokens": 4_000,
            "enabled": enabled,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- auth
def test_register_login_me_flow(client: TestClient) -> None:
    headers = register(client)
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.org"
    assert "password_hash" not in me.json()

    login = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASS})
    assert login.status_code == 200
    assert login.json()["access_token"]
    assert login.json()["token_type"] == "bearer"


def test_login_wrong_password_and_unknown_email_are_401(client: TestClient) -> None:
    register(client)
    bad = client.post(
        "/api/auth/login", json={"email": "owner@example.org", "password": "wrong-pass"}
    )
    assert bad.status_code == 401
    ghost = client.post("/api/auth/login", json={"email": "ghost@example.org", "password": PASS})
    assert ghost.status_code == 401
    assert bad.json()["error"]["message"] == ghost.json()["error"]["message"]


def test_duplicate_registration_is_409(client: TestClient) -> None:
    register(client)
    again = client.post("/api/auth/register", json={"email": "owner@example.org", "password": PASS})
    assert again.status_code == 409


def test_every_account_route_requires_auth(client: TestClient) -> None:
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/accounts", json={}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/agents/planner/binding").status_code == 401
    assert client.put("/api/agents/planner/binding", json={}).status_code == 401
    assert client.post("/api/accounts/x/test").status_code == 401
    assert client.delete("/api/accounts/x").status_code == 401


# --------------------------------------------------------------------------- CRUD
def test_account_crud_round_trip(client: TestClient, auth_headers: dict[str, str]) -> None:
    key = api_key("Crud1round2trip3")
    created = create_account(client, auth_headers, key=key)
    assert created["key_mask"] == "****" + key[-4:]
    assert "api_key" not in created and "key_ciphertext" not in created

    listed = client.get("/api/accounts", headers=auth_headers).json()
    assert [item["id"] for item in listed] == [created["id"]]

    patched = client.patch(
        f"/api/accounts/{created['id']}",
        json={"label": "renamed", "budget": {"usd": 10.0}},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["label"] == "renamed"
    assert patched.json()["budget"]["usd"] == 10.0

    deleted = client.delete(f"/api/accounts/{created['id']}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/api/accounts", headers=auth_headers).json() == []


def test_key_rotation_changes_fingerprint_not_visibility(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = create_account(client, auth_headers, key=api_key("First9key8seven7"))
    rotated_key = api_key("Rotated9key8seven")
    rotated = client.patch(
        f"/api/accounts/{created['id']}", json={"api_key": rotated_key}, headers=auth_headers
    ).json()
    assert rotated["key_fingerprint"] != created["key_fingerprint"]
    assert rotated["key_mask"] == "****" + rotated_key[-4:]


def test_create_rejects_subscription_token_and_returns_no_echo(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    oauth_key = "sk-ant-oat01-" + "A1b2" * 8
    response = client.post(
        "/api/accounts",
        json={"provider": "anthropic", "label": "bad", "api_key": oauth_key},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "Pro/Max" in response.text
    assert oauth_key not in response.text


def test_request_validation_never_echoes_the_secret_input(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A rejected oversized key must not come back in the 422 body.

    FastAPI's default validation body includes ``input``; for ``api_key`` that would
    turn a typo (a pasted key one character too long) into a response/log leak.
    """
    oversized = api_key("Z" * 600)
    response = client.post(
        "/api/accounts",
        json={"provider": "anthropic", "label": "too-long", "api_key": oversized},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "api_key" in response.text  # the location stays actionable
    assert oversized not in response.text
    assert "Z" * 40 not in response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_cross_user_denied_paths(client: TestClient) -> None:
    owner = register(client, "owner@example.org")
    thief = register(client, "thief@example.org")
    created = create_account(client, owner)
    account_id = created["id"]

    assert client.get(f"/api/accounts/{account_id}", headers=thief).status_code == 404
    assert (
        client.patch(f"/api/accounts/{account_id}", json={"label": "x"}, headers=thief).status_code
        == 404
    )
    assert client.delete(f"/api/accounts/{account_id}", headers=thief).status_code == 404
    assert client.post(f"/api/accounts/{account_id}/test", headers=thief).status_code == 404
    # A thief cannot even bind someone else's account.
    response = client.put(
        "/api/agents/writer/binding",
        json={
            "account_ids": [account_id],
            "strategy": "dedicated",
            "model": MODEL,
            "max_input_tokens": 24_000,
            "max_output_tokens": 4_000,
        },
        headers=thief,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- test endpoint
def test_test_endpoint_probes_the_credential(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = create_account(
        client, auth_headers, provider="mock", label="mock", key="mock-key-12345"
    )
    response = client.post(f"/api/accounts/{created['id']}/test", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error_code"] is None
    assert "api_key" not in response.text


# --------------------------------------------------------------------------- bindings
def test_binding_put_get_round_trip_including_planner(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    claude = create_account(client, auth_headers, label="claude")
    bound = bind(client, auth_headers, "planner", [claude["id"]])
    assert bound["role"] == "planner"
    fetched = client.get("/api/agents/planner/binding", headers=auth_headers).json()
    assert fetched["account_ids"] == [claude["id"]]
    assert fetched["model"] == MODEL
    missing = client.get("/api/agents/writer/binding", headers=auth_headers)
    assert missing.status_code == 404


def test_binding_validation_denied_paths(client: TestClient, auth_headers: dict[str, str]) -> None:
    claude = create_account(client, auth_headers, label="claude")
    router = create_account(
        client,
        auth_headers,
        provider="openai_compatible",
        label="router",
        key="sk-or-v1-sample-12345678",
        base_url="https://openrouter.example/api/v1",
    )
    # failover with a single account violates the frozen contract.
    response = client.put(
        "/api/agents/writer/binding",
        json={
            "account_ids": [claude["id"]],
            "strategy": "failover",
            "model": MODEL,
            "max_input_tokens": 24_000,
            "max_output_tokens": 4_000,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    # The planner must not run on a non-Claude account.
    response = client.put(
        "/api/agents/planner/binding",
        json={
            "account_ids": [router["id"]],
            "strategy": "dedicated",
            "model": MODEL,
            "max_input_tokens": 24_000,
            "max_output_tokens": 4_000,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "planner" in response.text
    # Unknown role values are rejected by the enum.
    assert client.get("/api/agents/chauffeur/binding", headers=auth_headers).status_code == 422


# --------------------------------------------------------------------------- delete protection
def test_delete_protected_without_flags(client: TestClient, auth_headers: dict[str, str]) -> None:
    account = create_account(client, auth_headers)
    bind(client, auth_headers, "writer", [account["id"]])
    response = client.delete(f"/api/accounts/{account['id']}", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "writer" in response.text
    assert client.get("/api/accounts", headers=auth_headers).json() != []


def test_delete_with_disable_roles(client: TestClient, auth_headers: dict[str, str]) -> None:
    account = create_account(client, auth_headers)
    bind(client, auth_headers, "writer", [account["id"]])
    response = client.delete(
        f"/api/accounts/{account['id']}?disable_roles=true", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["disabled_roles"] == ["writer"]
    binding = client.get("/api/agents/writer/binding", headers=auth_headers).json()
    assert binding["enabled"] is False


def test_delete_with_replacement_account(client: TestClient, auth_headers: dict[str, str]) -> None:
    old = create_account(client, auth_headers, label="old")
    new = create_account(client, auth_headers, label="new", key=api_key("Replacement12345"))
    bind(client, auth_headers, "reviewer", [old["id"]])
    response = client.delete(
        f"/api/accounts/{old['id']}",
        params={"replacement_account_id": new["id"]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["replaced_in"] == ["reviewer"]
    binding = client.get("/api/agents/reviewer/binding", headers=auth_headers).json()
    assert binding["account_ids"] == [new["id"]]


def test_delete_replacement_must_exist_and_differ(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    account = create_account(client, auth_headers)
    bind(client, auth_headers, "writer", [account["id"]])
    assert (
        client.delete(
            f"/api/accounts/{account['id']}",
            params={"replacement_account_id": account["id"]},
            headers=auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.delete(
            f"/api/accounts/{account['id']}",
            params={"replacement_account_id": "01HF2Z3K4M5N6P7Q8R9S0T1U2V"},
            headers=auth_headers,
        ).status_code
        == 404
    )
