"""Account-store errors.

They live in their own module so the store, the API and the provider pool can all
import them without importing the store itself (and creating an import cycle).
"""

from __future__ import annotations

from app.core.errors import NotFoundError, ScholarLoopError

__all__ = ["AccountNotFound", "AccountStoreError"]


class AccountStoreError(ScholarLoopError):
    """The account store could not satisfy a request (bad key, bad state)."""

    code = "account_store_error"
    http_status = 400


class AccountNotFound(NotFoundError):
    """No account with that id exists in the store."""

    code = "account_not_found"
    http_status = 404
