"""Minimal authentication: argon2 password hashes and short-lived JWTs.

Deliberately small: register, login, bearer-token check. Passwords are hashed with
argon2id (argon2-cffi defaults: memory-hard, per-hash salt, PHC-encoded string in
``users.password_hash``). JWTs are HS256 with an ``exp`` claim; the signing secret
comes from ``APP_JWT_SECRET`` and, failing that, is derived from the master key with
domain separation - never the raw master key itself. There is no password reset,
no refresh token and no session table: those are later steps' problems.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import AuthError

__all__ = [
    "EMAIL_PATTERN",
    "JWT_ALGORITHM",
    "MIN_PASSWORD_LENGTH",
    "AuthContext",
    "create_access_token",
    "decode_access_token",
    "get_jwt_secret",
    "hash_password",
    "verify_password",
]

#: Deliberately shape-only: no DNS check, no dependency on email-validator.
EMAIL_PATTERN: Final[str] = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LENGTH: Final[int] = 8
JWT_ALGORITHM: Final[str] = "HS256"
DEFAULT_TTL_SECONDS: Final[int] = 12 * 60 * 60
JWT_SECRET_ENV: Final[str] = "APP_JWT_SECRET"  # noqa: S105 (env var name, not a password)
JWT_SECRET_ENV_ALIAS: Final[str] = "SCHOLARLOOP_JWT_SECRET"  # noqa: S105 (env var name)
JWT_TTL_ENV: Final[str] = "APP_JWT_TTL_SECONDS"

_email_re = re.compile(EMAIL_PATTERN)
_hasher = PasswordHasher()


@dataclass(frozen=True)
class AuthContext:
    """Who a request is acting as, derived from a verified bearer token."""

    user_id: str
    email: str


def hash_password(password: str) -> str:
    """Argon2id PHC string. The plaintext is never stored, logged or returned."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verification; any malformed hash is simply "no match"."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def is_valid_email(email: str) -> bool:
    """True for a shape-valid email address."""
    return bool(_email_re.match(email))


def _ttl_seconds() -> int:
    raw = os.environ.get(JWT_TTL_ENV, "")
    if raw.strip().isdigit():
        return int(raw)
    return DEFAULT_TTL_SECONDS


def get_jwt_secret() -> str:
    """The HS256 signing secret.

    ``APP_JWT_SECRET`` wins. Without it, derive a distinct secret from the master
    key material with domain separation, so one leaked JWT never reveals the key
    that wraps account secrets (and vice versa).
    """
    explicit = os.environ.get(JWT_SECRET_ENV) or os.environ.get(JWT_SECRET_ENV_ALIAS)
    if explicit and explicit.strip():
        return explicit.strip()
    from app.accounts.crypto import get_master_key

    derived = get_master_key().derive("jwt-signing-v1")
    return derived.hex()


def create_access_token(user_id: str, email: str, *, now: float | None = None) -> str:
    """Mint a bearer token for ``user_id``. ``exp`` is enforced at decode time."""
    issued = time.time() if now is None else now
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(issued),
        "exp": int(issued) + _ttl_seconds(),
        "iss": "scholarloop",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> AuthContext:
    """Verify signature and expiry; map every failure to :class:`AuthError` (401)."""
    try:
        payload = jwt.decode(
            token,
            get_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
            leeway=5.0,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc}") from exc

    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise AuthError("invalid token: missing subject")
    email = payload.get("email")
    return AuthContext(user_id=user_id, email=email if isinstance(email, str) else "")
