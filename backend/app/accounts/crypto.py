"""AES-GCM sealing for provider keys (HARD RULE 2).

The account store never keeps a key in the clear. This module is the only place
where a plaintext key and a stored blob meet:

* :meth:`SecretBox.seal` turns a key into ``v1:<base64(nonce + ciphertext + tag)>``;
* :meth:`SecretBox.open` turns it back, and *fails loudly* when the blob was
  tampered with, truncated, or sealed with a different secret.

The encryption key is derived from ``SCHOLARLOOP_SECRET_KEY`` with HKDF-SHA256
(fixed salt and info strings), so the same secret always opens the same blob and a
different secret never does. The nonce is random per seal, which is why the
*fingerprint* of an account changes every time its key is re-encrypted: that is
exactly the "the key changed" signal the UI shows (see
:func:`app.contracts.account.key_fingerprint`).

Nothing here logs. A :class:`SecretBoxError` message never contains the key or the
blob, only the reason the operation failed.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from app.core.errors import ScholarLoopError

__all__ = ["CIPHER_VERSION", "KEY_BYTES", "NONCE_BYTES", "SecretBox", "SecretBoxError"]

#: Version tag of the stored format. Bumping it invalidates old blobs on purpose.
CIPHER_VERSION: Final = "v1"
#: AES-256 key length.
KEY_BYTES: Final = 32
#: GCM nonce length (96 bits is the size every GCM implementation likes).
NONCE_BYTES: Final = 12

#: Fixed HKDF salt: the secret is already high-entropy, so the salt only binds the
#: derivation to *this* application.
_KDF_SALT: Final = b"scholarloop.account-store.v1"
#: Fixed HKDF info: separates this key from any other use of the same secret.
_KDF_INFO: Final = b"scholarloop.account-key.v1"


class SecretBoxError(ScholarLoopError):
    """A stored key blob could not be opened."""

    code = "secret_box_error"
    http_status = 500


class SecretBox:
    """Seal and open provider keys with AES-256-GCM.

    The object is immutable and safe to share: it holds the derived key and the
    AEAD cipher, never a provider key.
    """

    def __init__(self, secret: str | SecretStr) -> None:
        """Derive the encryption key from ``secret``.

        Args:
            secret: the deployment secret (``SCHOLARLOOP_SECRET_KEY``). It may be a
                :class:`pydantic.SecretStr`, so a caller can pass the settings field
                without unwrapping it into a local variable.

        Raises:
            SecretBoxError: the secret is empty - an account store without a secret
                cannot encrypt anything and must not pretend to.
        """
        material = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if not material.strip():
            raise SecretBoxError("the account store secret is empty")
        self._key = HKDF(
            algorithm=SHA256(),
            length=KEY_BYTES,
            salt=_KDF_SALT,
            info=_KDF_INFO,
        ).derive(material.encode("utf-8"))
        self._aead = AESGCM(self._key)

    def seal(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return a storable ``v1:...`` blob."""
        nonce = os.urandom(NONCE_BYTES)
        blob = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return f"{CIPHER_VERSION}:{base64.urlsafe_b64encode(nonce + blob).decode('ascii')}"

    def open(self, ciphertext: str) -> str:
        """Decrypt a ``v1:...`` blob.

        Raises:
            SecretBoxError: unknown format, invalid base64, truncation, or a failed
                authentication tag. The message never echoes the blob.
        """
        version, separator, payload = ciphertext.partition(":")
        if not separator or version != CIPHER_VERSION or not payload:
            raise SecretBoxError(f"unsupported ciphertext format (expected {CIPHER_VERSION}:...)")

        try:
            raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        except (ValueError, binascii.Error) as exc:
            raise SecretBoxError("ciphertext is not valid base64") from exc

        if len(raw) <= NONCE_BYTES:
            raise SecretBoxError("ciphertext is truncated")

        nonce, blob = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, blob, None).decode("utf-8")
        except InvalidTag as exc:
            raise SecretBoxError("ciphertext failed authentication") from exc
