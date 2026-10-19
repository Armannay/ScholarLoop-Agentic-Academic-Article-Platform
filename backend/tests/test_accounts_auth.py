"""Argon2 password hashing and JWT bearer tokens (register/login/verify path)."""

from __future__ import annotations

import time

import pytest

from app.accounts.auth import (
    AuthContext,
    create_access_token,
    decode_access_token,
    get_jwt_secret,
    hash_password,
    is_valid_email,
    verify_password,
)
from app.core.errors import AuthError, ConfigError

pytestmark = pytest.mark.accounts


def test_password_hash_round_trip_and_wrong_password() -> None:
    passphrase = "correct-horse-battery-staple"
    phc = hash_password(passphrase)
    assert phc.startswith("$argon2")
    assert passphrase not in phc
    assert verify_password(phc, passphrase) is True
    assert verify_password(phc, "wrong") is False
    assert verify_password("not-a-hash", passphrase) is False


def test_email_shape_validation() -> None:
    assert is_valid_email("a@b.co")
    assert not is_valid_email("nope")
    assert not is_valid_email("a@b")
    assert not is_valid_email("@b.co")


def test_jwt_round_trip(app_master_key_env: str) -> None:
    token = create_access_token("01HF2Z3K4M5N6P7Q8R9S0T1U2V", "a@b.co")
    context = decode_access_token(token)
    assert context == AuthContext(user_id="01HF2Z3K4M5N6P7Q8R9S0T1U2V", email="a@b.co")


def test_jwt_rejects_expired_and_tampered(app_master_key_env: str) -> None:
    expired = create_access_token("01HF2Z3K4M5N6P7Q8R9S0T1U2V", "a@b.co", now=time.time() - 86_400)
    with pytest.raises(AuthError, match="expired"):
        decode_access_token(expired)
    good = create_access_token("01HF2Z3K4M5N6P7Q8R9S0T1U2V", "a@b.co")
    tampered = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    with pytest.raises(AuthError, match="invalid token"):
        decode_access_token(tampered)


def test_jwt_secret_env_wins_and_derivation_needs_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_JWT_SECRET", "explicit-test-secret-value")
    assert get_jwt_secret() == "explicit-test-secret-value"
    monkeypatch.delenv("APP_JWT_SECRET")
    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    monkeypatch.delenv("SCHOLARLOOP_MASTER_KEY", raising=False)
    with pytest.raises(ConfigError):
        get_jwt_secret()
    monkeypatch.setenv("APP_MASTER_KEY", "ab" * 32)
    derived = get_jwt_secret()
    assert derived != "ab" * 32
    assert len(derived) == 64  # hex of the derived 32 bytes


def test_derived_jwt_secret_is_not_the_master_key(
    monkeypatch: pytest.MonkeyPatch, master_key_raw: str
) -> None:
    monkeypatch.setenv("APP_MASTER_KEY", master_key_raw)
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    derived = get_jwt_secret()
    assert derived != master_key_raw
    # Domain separation: purpose "jwt-signing-v1" is distinct from the KEK use.
    from app.accounts.crypto import MasterKey

    assert bytes.fromhex(derived) == MasterKey.from_string(master_key_raw).derive("jwt-signing-v1")
