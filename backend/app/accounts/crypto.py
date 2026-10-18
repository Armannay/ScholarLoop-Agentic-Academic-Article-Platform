"""Envelope encryption for provider API keys (HARD RULE 2).

Every plaintext key is wrapped twice before it touches storage:

1. a fresh random **data key (DEK)** encrypts the API key with AES-256-GCM, bound to
   the account id as AAD (a ciphertext cannot be moved between rows);
2. the **master key (KEK)** from ``APP_MASTER_KEY`` encrypts that DEK.

So a leaked database row is worthless without the master key, and rotating the
master key re-wraps data keys instead of forcing every user to re-enter credentials.
``rotate_master_key`` goes further and re-encrypts every row end to end.

Blob format (compact, versioned, base64url-safe chars only)::

    v1.<b64(nonce)>.<b64(ciphertext)>.<b64(dek_nonce)>.<b64(wrapped_dek)>

This module also owns two last-line defences: :func:`mask_secret` (the only display
form a key ever takes) and :class:`KeyRedactionFilter` (pattern + known-key
redaction for every log handler). Nothing above this module ever handles a
plaintext key except to hand it to a provider adapter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets as _secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.errors import ConfigError
from app.core.redaction import REDACTED, redact

__all__ = [
    "BLOB_PREFIX",
    "KEY_MIN_LENGTH",
    "KeyRedactionFilter",
    "MasterKey",
    "RotationReport",
    "forget_secret",
    "generate_master_key",
    "get_master_key",
    "install_key_redaction",
    "mask_secret",
    "redact_keys",
    "register_secret",
    "rotate_master_key",
]

#: Version tag of the envelope format. Bump on any layout change.
BLOB_PREFIX: Final[str] = "v1"
_MASTER_KEY_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 12
#: Provider keys shorter than this are almost certainly truncated paste errors.
KEY_MIN_LENGTH: Final[int] = 8
_KEY_MAX_LENGTH: Final[int] = 512
#: Cap the known-secret registry: a registry that can grow without bound is its own
#: leak vector. At 1024 entries it covers any realistic multi-tenant deployment.
_REGISTRY_MAX: Final[int] = 1024

#: Environment variable naming the master key (checked first).
MASTER_KEY_ENV: Final[str] = "APP_MASTER_KEY"
#: Backwards-compatible alias with the rest of the SCHOLARLOOP_* settings.
MASTER_KEY_ENV_ALIAS: Final[str] = "SCHOLARLOOP_MASTER_KEY"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- master key
class MasterKey:
    """A 32-byte AES key held only in process memory."""

    __slots__ = ("_key",)

    def __init__(self, key_bytes: bytes) -> None:
        if len(key_bytes) != _MASTER_KEY_BYTES:
            raise ConfigError(f"master key must be {_MASTER_KEY_BYTES} bytes, got {len(key_bytes)}")
        self._key = key_bytes

    @classmethod
    def from_string(cls, raw: str) -> MasterKey:
        """Parse a master key from an environment-style string.

        Accepted shapes: 64-char hex, base64/base64url of exactly 32 bytes, or a
        passphrase of at least 32 characters (hashed with SHA-256). Generate one
        with :func:`generate_master_key`.
        """
        text = raw.strip()
        if not text:
            raise ConfigError("master key is empty")
        try:
            if re.fullmatch(r"[0-9a-fA-F]{64}", text):
                return cls(bytes.fromhex(text))
            decoded = _b64d(text)
            if len(decoded) == _MASTER_KEY_BYTES:
                return cls(decoded)
        except ValueError as exc:  # malformed base64 - fall through to passphrase
            raise ConfigError(f"master key is not valid hex/base64: {exc}") from exc
        if len(text) >= 32:
            return cls(hashlib.sha256(text.encode("utf-8")).digest())
        raise ConfigError(
            "master key must be 64-char hex, 32-byte base64, or a passphrase of 32+ characters"
        )

    @classmethod
    def from_env(cls) -> MasterKey:
        """Read ``APP_MASTER_KEY`` (or the SCHOLARLOOP_ alias) from the environment."""
        raw = os.environ.get(MASTER_KEY_ENV) or os.environ.get(MASTER_KEY_ENV_ALIAS)
        if not raw:
            raise ConfigError(
                f"{MASTER_KEY_ENV} is not set; the encrypted account store is unusable. "
                f'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return cls.from_string(raw)

    def fingerprint(self) -> str:
        """Short non-reversible id of the master key, for rotation logs."""
        return hashlib.sha256(self._key).hexdigest()[:12].upper()

    def derive(self, purpose: str) -> bytes:
        """Derive a purpose-separated 32-byte subkey (KDF over the raw key material).

        Distinct purposes yield independent keys: the JWT signing secret and the
        account-store KEK are never the same bytes, and neither is ever exposed.
        """
        return hashlib.sha256(self._key + b"|scholarloop|" + purpose.encode("utf-8")).digest()

    def __repr__(self) -> str:  # pragma: no cover - defensive, not a code path
        return f"MasterKey(fp={self.fingerprint()})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MasterKey) and other._key == self._key

    def __hash__(self) -> int:
        return hash(self._key)


def generate_master_key() -> str:
    """A fresh master key as urlsafe base64, ready for ``APP_MASTER_KEY``."""
    return _b64e(os.urandom(_MASTER_KEY_BYTES))


def get_master_key() -> MasterKey:
    """The process master key from the environment (cached by the caller)."""
    return MasterKey.from_env()


# --------------------------------------------------------------------------- envelope
def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


class EnvelopeEncryptor:
    """Encrypts and decrypts account secrets with one master key."""

    def __init__(self, master_key: MasterKey) -> None:
        self._master = master_key

    @property
    def master_key(self) -> MasterKey:
        return self._master

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        """Wrap ``plaintext`` (the provider key) into a ``v1`` envelope blob.

        ``aad`` must be the account id: the binding of ciphertext to row is part of
        the authenticated data, so a blob copied to another account fails to open.
        """
        if not (KEY_MIN_LENGTH <= len(plaintext) <= _KEY_MAX_LENGTH):
            raise ValueError(f"secret must be {KEY_MIN_LENGTH}..{_KEY_MAX_LENGTH} chars")
        dek = _secrets.token_bytes(_MASTER_KEY_BYTES)
        aad_bytes = aad.encode("utf-8")
        nonce, ciphertext = _encrypt(dek, plaintext.encode("utf-8"), aad_bytes)
        wrapped_nonce, wrapped_dek = _encrypt(self._master._key, dek, aad_bytes)
        blob = ".".join(
            (BLOB_PREFIX, _b64e(nonce), _b64e(ciphertext), _b64e(wrapped_nonce), _b64e(wrapped_dek))
        )
        register_secret(plaintext)
        return blob

    def decrypt(self, blob: str, *, aad: str) -> str:
        """Open a ``v1`` blob. The returned key is registered for log redaction."""
        plaintext = self._decode(blob, aad=aad, master=self._master)
        register_secret(plaintext)
        return plaintext

    @staticmethod
    def _decode(blob: str, *, aad: str, master: MasterKey) -> str:
        parts = blob.split(".")
        if len(parts) != 5 or parts[0] != BLOB_PREFIX:
            raise ValueError(f"not a {BLOB_PREFIX} envelope blob")
        nonce, ciphertext, wrapped_nonce, wrapped_dek = (_b64d(part) for part in parts[1:])
        aad_bytes = aad.encode("utf-8")
        try:
            dek = _decrypt(master._key, wrapped_nonce, wrapped_dek, aad_bytes)
            return _decrypt(dek, nonce, ciphertext, aad_bytes).decode("utf-8")
        except InvalidTag as exc:
            # Wrong master key, wrong row (AAD), or tampered bytes - all one message:
            # distinguish nothing, learn nothing.
            raise ValueError(
                "envelope decryption failed (wrong master key or tampered blob)"
            ) from exc


@dataclass(frozen=True)
class RotationReport:
    """Outcome of one master-key rotation sweep."""

    rotated: int
    skipped: int
    old_fingerprint: str
    new_fingerprint: str


def rotate_master_key(
    session: object,
    *,
    new_key: MasterKey,
    old_key: MasterKey | None = None,
    actor: str = "system",
) -> RotationReport:
    """Re-encrypt every account's key under ``new_key``. Everything, in one sweep.

    Decrypts each row with the old key and re-encrypts with a fresh DEK under the
    new key (full re-encryption, not just re-wrapping). Rows that cannot be opened
    with the old key are counted as ``skipped`` and left untouched, so one bad row
    cannot block a rotation. The caller's transaction is used as-is; this function
    only marks rows dirty.

    Args:
        session: a SQLAlchemy session (typed loosely to keep crypto free of DB
            imports at module load; the account store passes a real Session).
        new_key: the master key to rotate to.
        old_key: the current master key; defaults to the environment value.
        actor: recorded in the audit entry written for the rotation.
    """
    # Imported lazily: crypto stays importable without the DB stack (tests, tools).
    from app.accounts.models import AccountRow, AuditLogRow
    from app.core.ids import new_ulid

    old = old_key if old_key is not None else get_master_key()
    new_enc = EnvelopeEncryptor(new_key)

    rotated = 0
    skipped = 0
    rows = session.query(AccountRow).all()  # type: ignore[attr-defined]
    for row in rows:
        try:
            plaintext = EnvelopeEncryptor._decode(row.key_ciphertext, aad=row.id, master=old)
        except ValueError:
            skipped += 1
            continue
        row.key_ciphertext = new_enc.encrypt(plaintext, aad=row.id)
        forget_secret(plaintext)
        rotated += 1

    session.add(  # type: ignore[attr-defined]
        AuditLogRow(
            id=new_ulid(),
            actor=actor,
            action="master_key.rotate",
            target="account_store",
            ts=_utc_now(),
            detail=json.dumps(
                {"rotated": rotated, "skipped": skipped, "new_fp": new_key.fingerprint()},
                sort_keys=True,
            ),
        )
    )
    return RotationReport(
        rotated=rotated,
        skipped=skipped,
        old_fingerprint=old.fingerprint(),
        new_fingerprint=new_key.fingerprint(),
    )


def _utc_now() -> datetime:
    from app.core.clock import utc_now

    return utc_now()


# --------------------------------------------------------------------------- masking
def mask_secret(secret: str) -> str:
    """The only display form a key ever takes: ``****`` plus its last 4 characters.

    Short inputs are masked entirely - a 3-character "last 4" would be the key.
    """
    if len(secret) <= 4:
        return "*" * max(len(secret), 4)
    return "*" * 4 + secret[-4:]


# --------------------------------------------------------------------------- redaction
_registry: OrderedDict[str, None] = OrderedDict()


def register_secret(secret: str) -> None:
    """Track a plaintext secret so :func:`redact_keys` can scrub it from logs.

    Every decrypted key is registered automatically by the encryptor; providers
    and tests may register extra material (passwords, tokens) the same way.
    """
    if not secret:
        return
    _registry[secret] = None
    _registry.move_to_end(secret)
    while len(_registry) > _REGISTRY_MAX:
        _registry.popitem(last=False)


def forget_secret(secret: str) -> None:
    """Drop a secret from the redaction registry (key rotated, account deleted)."""
    _registry.pop(secret, None)


def registered_secrets() -> tuple[str, ...]:
    """Snapshot of the registry - for tests and diagnostics only."""
    return tuple(_registry)


def redact_keys(text: str) -> str:
    """Redact key-shaped strings *and* every specifically known secret."""
    out = redact(text)
    for secret in _registry:
        if secret in out:
            out = out.replace(secret, REDACTED)
    return out


class KeyRedactionFilter(logging.Filter):
    """Logging filter: key-shaped strings and known secrets become ``[REDACTED]``.

    Strictly stronger than pattern matching alone: a key that matches no vendor
    pattern (OpenRouter-style ids, future formats) is still scrubbed, because the
    account store registered it when it decrypted it. Attach with
    :func:`install_key_redaction` after :func:`app.core.logging.configure_logging`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record must not crash logging
            return True
        redacted = redact_keys(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_text:
            record.exc_text = redact_keys(record.exc_text)
        return True


def install_key_redaction() -> int:
    """Attach :class:`KeyRedactionFilter` to every root handler. Idempotent.

    Returns the number of handlers now carrying the filter.
    """
    root = logging.getLogger()
    count = 0
    for handler in root.handlers:
        if not any(isinstance(f, KeyRedactionFilter) for f in handler.filters):
            handler.addFilter(KeyRedactionFilter())
        count += 1
    return count
