"""Envelope encryption, key rotation, masking and the key-redaction filter.

Every test here is about one property: plaintext key material is usable exactly
where it must be and invisible everywhere else. Synthetic keys are assembled at
runtime (see ``api_key()``) so the repo's secret scanner stays honest about what
is a real credential - and so the redaction patterns have something real to catch.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.accounts.crypto import (
    EnvelopeEncryptor,
    KeyRedactionFilter,
    MasterKey,
    forget_secret,
    generate_master_key,
    install_key_redaction,
    mask_secret,
    redact_keys,
    register_secret,
    registered_secrets,
    rotate_master_key,
)
from app.accounts.models import AccountRow, AuditLogRow, UserRow
from app.core.errors import ConfigError
from app.core.ids import new_ulid
from app.core.redaction import REDACTED

pytestmark = pytest.mark.security


def api_key(tail: str = "A1b2C3d4E5f6G7h8") -> str:
    """A key shaped like a Claude Console key, built at runtime."""
    return "sk-ant-api03-" + tail


def test_master_key_from_string_accepts_hex_base64_and_passphrase() -> None:
    raw = generate_master_key()
    as_base64 = MasterKey.from_string(raw)
    as_hex = MasterKey.from_string(as_base64._key.hex())
    as_phrase = MasterKey.from_string("a-sufficiently-long-passphrase-for-dev-only")
    assert as_base64 == as_hex
    assert as_phrase != as_base64
    with pytest.raises(ConfigError):
        MasterKey.from_string("short")
    with pytest.raises(ConfigError):
        MasterKey.from_string("")


def test_master_key_from_env_prefers_app_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MASTER_KEY", generate_master_key())
    assert MasterKey.from_env().fingerprint() == MasterKey.from_env().fingerprint()
    monkeypatch.delenv("APP_MASTER_KEY")
    monkeypatch.setenv("SCHOLARLOOP_MASTER_KEY", generate_master_key())
    assert MasterKey.from_env() is not None
    monkeypatch.delenv("SCHOLARLOOP_MASTER_KEY")
    with pytest.raises(ConfigError):
        MasterKey.from_env()


def test_envelope_round_trip(master_key: MasterKey) -> None:
    encryptor = EnvelopeEncryptor(master_key)
    key = api_key()
    blob = encryptor.encrypt(key, aad="01HF2Z3K4M5N6P7Q8R9S0T1U2V")
    assert key not in blob
    assert blob.startswith("v1.")
    assert encryptor.decrypt(blob, aad="01HF2Z3K4M5N6P7Q8R9S0T1U2V") == key


def test_envelope_rejects_wrong_key_wrong_aad_and_tampering(master_key: MasterKey) -> None:
    encryptor = EnvelopeEncryptor(master_key)
    key = api_key()
    blob = encryptor.encrypt(key, aad="account-A")
    other_master = EnvelopeEncryptor(MasterKey.from_string(generate_master_key()))
    with pytest.raises(ValueError, match="decryption failed"):
        other_master.decrypt(blob, aad="account-A")
    with pytest.raises(ValueError, match="decryption failed"):
        encryptor.decrypt(blob, aad="account-B")  # AAD binds the blob to its row
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError):
        encryptor.decrypt(tampered, aad="account-A")


def test_envelope_rejects_malformed_blob(master_key: MasterKey) -> None:
    encryptor = EnvelopeEncryptor(master_key)
    with pytest.raises(ValueError, match="envelope blob"):
        encryptor.decrypt("not-a-blob", aad="x")


def test_encrypt_registers_the_plaintext_for_redaction(master_key: MasterKey) -> None:
    encryptor = EnvelopeEncryptor(master_key)
    key = api_key("Zz9y8x7w6v5u4t3s")
    try:
        encryptor.encrypt(key, aad="acc")
        assert key in registered_secrets()
        assert redact_keys(f"the key is {key} !") == f"the key is {REDACTED} !"
    finally:
        forget_secret(key)


def test_mask_secret_shows_only_the_last_four() -> None:
    assert mask_secret("sk-ant-api03-abcdefgh") == "****" + "efgh"
    assert mask_secret("abcd") == "****"
    assert mask_secret("x") == "****"
    assert "sk-ant" not in mask_secret(api_key())


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_key_redaction_filter_scrubs_patterns_and_registered_keys() -> None:
    handler = _RecordingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(KeyRedactionFilter())
    logger = logging.getLogger("test.keyredaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        key = api_key("Qq1w2e3r4t5y6u7i")
        register_secret(key)
        pattern_key = "sk-proj-" + "A1" * 20  # matches the openai pattern
        logger.info("rotating %s now %s end", key, pattern_key)
        text = "\n".join(handler.lines)
        assert key not in text
        assert pattern_key not in text
        assert REDACTED in text
    finally:
        forget_secret(key)
        logger.removeHandler(handler)


def test_install_key_redaction_is_idempotent() -> None:
    handler = _RecordingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert install_key_redaction() >= 1
        first = [f for f in handler.filters if isinstance(f, KeyRedactionFilter)]
        install_key_redaction()
        second = [f for f in handler.filters if isinstance(f, KeyRedactionFilter)]
        assert len(first) == 1 and len(second) == 1
    finally:
        root.removeHandler(handler)


def test_rotate_master_key_reencrypts_every_row(session_factory, master_key: MasterKey) -> None:
    from app.accounts.db import session_scope

    other_master = MasterKey.from_string(generate_master_key())
    keys = {"acc1": api_key("1111222233334444"), "acc2": api_key("5555666677778888")}
    old_enc = EnvelopeEncryptor(master_key)

    with session_scope(session_factory) as session:
        session.add(
            UserRow(id=new_ulid(), email="rot@example.org", password_hash="x", created_at=_now())
        )
        owner = session.scalar(select(UserRow)).id
        for suffix, key in keys.items():
            account_id = new_ulid()
            session.add(
                AccountRow(
                    id=account_id,
                    owner_id=owner,
                    provider="anthropic",
                    label=suffix,
                    key_ciphertext=old_enc.encrypt(key, aad=account_id),
                    key_mask=mask_secret(key),
                    status="active",
                    created_at=_now(),
                    updated_at=_now(),
                )
            )

    with session_scope(session_factory) as session:
        report = rotate_master_key(session, new_key=other_master, old_key=master_key)
        assert report.rotated == 2
        assert report.skipped == 0

    # After rotation only the new master opens the rows.
    new_enc = EnvelopeEncryptor(other_master)
    with session_scope(session_factory) as session:
        rows = session.scalars(select(AccountRow)).all()
        for row in rows:
            with pytest.raises(ValueError, match="decryption failed"):
                EnvelopeEncryptor(master_key).decrypt(row.key_ciphertext, aad=row.id)
            plaintext = new_enc.decrypt(row.key_ciphertext, aad=row.id)
            assert plaintext in keys.values()
        audits = session.scalars(select(AuditLogRow)).all()
        assert any(a.action == "master_key.rotate" for a in audits)


def test_audit_rows_never_store_secret_shapes(session_factory) -> None:
    from app.accounts.db import session_scope
    from app.accounts.service import AuditService

    key = api_key("Nn0m9k8j7h6g5f4d")
    with session_scope(session_factory) as session:
        AuditService(session).record(
            actor="u",
            action="account.rotate_key",
            target="a",
            detail={"oops": f"leaked {key}"},
        )
    with session_scope(session_factory) as session:
        row = session.scalar(select(AuditLogRow))
        assert row is not None
        assert row.detail is not None
        assert key not in row.detail
        assert REDACTED in row.detail


def _now():
    from app.core.clock import utc_now

    return utc_now()
