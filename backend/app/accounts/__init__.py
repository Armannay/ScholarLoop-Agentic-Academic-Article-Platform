"""Provider accounts: encrypted at rest, resolved at call time.

Three pieces, deliberately separate:

* :mod:`app.accounts.crypto` - AES-GCM sealing of a key (``SecretBox``);
* :mod:`app.accounts.errors` - the store's own error family;
* :mod:`app.accounts.store` - the :class:`AccountStore` protocol and the shipped
  in-memory implementation.

The provider pool (see :mod:`app.providers.pool`) depends on the protocol only, so
a durable store can replace the in-memory one without touching a caller.
"""

from __future__ import annotations

from app.accounts.crypto import CIPHER_VERSION, KEY_BYTES, NONCE_BYTES, SecretBox, SecretBoxError
from app.accounts.errors import AccountNotFound, AccountStoreError
from app.accounts.store import MOCK_KEY_PLACEHOLDER, AccountStore, InMemoryAccountStore

__all__ = [
    "CIPHER_VERSION",
    "KEY_BYTES",
    "MOCK_KEY_PLACEHOLDER",
    "NONCE_BYTES",
    "AccountNotFound",
    "AccountStore",
    "AccountStoreError",
    "InMemoryAccountStore",
    "SecretBox",
    "SecretBoxError",
]
