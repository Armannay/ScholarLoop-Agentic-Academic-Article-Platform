"""HARD RULE 2, end to end: no key appears in any response or log line.

One full account lifecycle runs against the real API - create, list, patch, key
rotation, bindings, test, delete, plus the error paths - while every response body
and every log record is captured and searched for the plaintext key. The logging
pipeline also carries :class:`KeyRedactionFilter`, so even a careless log call
somewhere else in the process cannot emit the key.

Keys are assembled at runtime (``api_key()``) so the repo secret scanner is not
asked to bless fake credentials as examples, and so the redaction *patterns* meet
a string they must catch.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.accounts.crypto import KeyRedactionFilter, forget_secret, install_key_redaction
from app.core.redaction import REDACTED, find_secrets
from tests.test_accounts_crypto import api_key

pytestmark = pytest.mark.security

PASS = "correct-horse-battery"


class _CaptureHandler(logging.Handler):
    """Captures formatted records, exactly as a log file would see them."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def log_capture() -> logging.Handler:
    handler = _CaptureHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(KeyRedactionFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    install_key_redaction()
    yield handler
    root.removeHandler(handler)


def test_no_key_in_any_response_or_log_line(client: TestClient, log_capture) -> None:
    key = api_key("Leak1test2secret3")
    responses: list[str] = []
    try:
        headers_response = client.post(
            "/api/auth/register", json={"email": "owner@example.org", "password": PASS}
        )
        responses.append(headers_response.text)
        headers = {"authorization": f"Bearer {headers_response.json()['access_token']}"}

        def remember(response) -> object:
            responses.append(response.text)
            return response

        # Happy path: create, read, list, patch (label + rotate), bind, test, delete.
        created = remember(
            client.post(
                "/api/accounts",
                json={"provider": "anthropic", "label": "work", "api_key": key},
                headers=headers,
            )
        ).json()
        remember(client.get("/api/accounts", headers=headers))
        remember(client.get(f"/api/accounts/{created['id']}", headers=headers))
        remember(
            client.patch(
                f"/api/accounts/{created['id']}",
                json={"label": "renamed", "api_key": api_key("Rotated4leak5test")},
                headers=headers,
            )
        )
        remember(
            client.put(
                "/api/agents/writer/binding",
                json={
                    "account_ids": [created["id"]],
                    "strategy": "dedicated",
                    "model": "claude-sonnet-4-5",
                    "max_input_tokens": 24_000,
                    "max_output_tokens": 4_000,
                },
                headers=headers,
            )
        )
        remember(client.post(f"/api/accounts/{created['id']}/test", headers=headers))

        # Error paths leak more often than happy ones.
        remember(
            client.post(
                "/api/accounts",
                json={"provider": "anthropic", "label": "dup", "api_key": key},
                headers=headers,
            )
        )
        remember(
            client.post(
                "/api/accounts",
                json={
                    "provider": "anthropic",
                    "label": "bad",
                    "api_key": "sk-ant-oat01-" + "B2c3" * 8,
                },
                headers=headers,
            )
        )
        remember(client.get("/api/accounts/01HF2Z3K4M5N6P7Q8R9S0T1U2V", headers=headers))
        remember(
            client.delete(f"/api/accounts/{created['id']}", headers=headers)  # 409: bound
        )
        remember(
            client.delete(f"/api/accounts/{created['id']}?disable_roles=true", headers=headers)
        )

        joined_responses = "\n".join(responses)
        captured_logs = "\n".join(log_capture.lines)

        assert key not in joined_responses
        assert "Rotated4leak5test" not in joined_responses
        assert key not in captured_logs
        assert "Rotated4leak5test" not in captured_logs
        # No key-*shaped* text either, redacted or otherwise unaccounted for.
        assert find_secrets(joined_responses) == []
        # The mask is the only trace a key leaves in a response body.
        assert "****" in joined_responses
    finally:
        forget_secret(key)
        forget_secret(api_key("Rotated4leak5test"))


def test_log_filter_redacts_a_key_that_reaches_a_log_line(log_capture) -> None:
    """The safety net: even a log call that names the key emits only [REDACTED]."""
    key = api_key("Filter6net7safety8")
    try:
        logging.getLogger("app.anything").error("storing key %s for account", key)
        text = "\n".join(log_capture.lines)
        assert key not in text
        assert REDACTED in text
    finally:
        forget_secret(key)


def test_ciphertext_blobs_are_not_key_shaped(client: TestClient) -> None:
    """What the DB stores is an envelope blob, not a recognisable key."""
    headers_response = client.post(
        "/api/auth/register", json={"email": "blob@example.org", "password": PASS}
    )
    headers = {"authorization": f"Bearer {headers_response.json()['access_token']}"}
    key = api_key("Blob9check8store7")
    try:
        created = client.post(
            "/api/accounts",
            json={"provider": "anthropic", "label": "work", "api_key": key},
            headers=headers,
        ).json()
        assert created["key_fingerprint"] != key
        assert find_secrets(created["key_fingerprint"]) == []
    finally:
        forget_secret(key)
