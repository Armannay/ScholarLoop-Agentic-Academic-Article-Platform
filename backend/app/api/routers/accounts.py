"""Account administration: add, list, change, rotate and delete provider accounts.

Users manage their own provider accounts while the platform runs (ADR-0004), so
these endpoints exist from the start. Three rules shape the surface:

* **a key is write-only.** It arrives in a request body, is encrypted by the
  account store and is never returned. Every response carries a
  :class:`~app.contracts.account.AccountPublic`, whose ``key_fingerprint`` proves
  *that* a key was set, not *which* key it is;
* **validation errors never echo the body.** The application installs a
  validation handler that drops the offending value, so a malformed key cannot
  come back in a 422 body;
* **the store may be disabled.** Without ``SCHOLARLOOP_SECRET_KEY`` there is no
  way to encrypt a key, so the endpoints answer 503 with a clear reason instead
  of pretending to store a plaintext key.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.accounts.errors import AccountNotFound
from app.accounts.store import AccountStore
from app.api.deps import AccountStoreDep
from app.api.schemas import AccountCreateRequest, AccountKeyRequest, AccountUpdateRequest
from app.contracts.account import AccountPublic
from app.contracts.enums import AccountProvider, AccountStatus

__all__ = ["router"]

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _public(account_id: str, store: AccountStore) -> AccountPublic:
    """The redacted view of one account, or 404."""
    account = store.get(account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": AccountNotFound.code, "message": f"no account with id {account_id}"},
        )
    return account.public()


@router.get("", response_model=list[AccountPublic], summary="List provider accounts")
def list_accounts(
    store: AccountStoreDep,
    owner_id: Annotated[str | None, Query(max_length=64)] = None,
    provider: Annotated[AccountProvider | None, Query()] = None,
    account_status: Annotated[AccountStatus | None, Query(alias="status")] = None,
) -> list[AccountPublic]:
    """Every account matching the filters, as redacted views."""
    accounts = store.list_accounts(owner_id=owner_id, provider=provider, status=account_status)
    return [account.public() for account in accounts]


@router.post(
    "",
    response_model=AccountPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Add a provider account",
)
def create_account(payload: AccountCreateRequest, store: AccountStoreDep) -> AccountPublic:
    """Add an account. The key is encrypted before it is stored and never returned."""
    account = store.add(
        owner_id=payload.owner_id,
        provider=payload.provider,
        label=payload.label,
        key=payload.key.get_secret_value() if payload.key is not None else None,
        base_url=payload.base_url,
        monthly_budget=payload.monthly_budget,
    )
    return account.public()


@router.get("/{account_id}", response_model=AccountPublic, summary="Show one account")
def get_account(account_id: str, store: AccountStoreDep) -> AccountPublic:
    """One account, redacted."""
    return _public(account_id, store)


@router.patch("/{account_id}", response_model=AccountPublic, summary="Change an account")
def update_account(
    account_id: str,
    payload: AccountUpdateRequest,
    store: AccountStoreDep,
) -> AccountPublic:
    """Change the label, the status or the monthly budget of an account."""
    account = store.require(account_id)
    if payload.label is not None or payload.monthly_budget is not None:
        account = store.update_metadata(
            account_id,
            label=payload.label,
            monthly_budget=payload.monthly_budget,
        )
    if payload.status is not None:
        account = store.set_status(account_id, payload.status)
    return account.public()


@router.put("/{account_id}/key", response_model=AccountPublic, summary="Rotate the key")
def rotate_key(
    account_id: str,
    payload: AccountKeyRequest,
    store: AccountStoreDep,
) -> AccountPublic:
    """Replace the key of an account. The fingerprint changes, the key does not leak."""
    account = store.replace_key(account_id, payload.key.get_secret_value())
    return account.public()


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an account")
def delete_account(account_id: str, store: AccountStoreDep) -> Response:
    """Delete an account. Deleting an unknown account is a 404, not a silent success."""
    store.require(account_id)
    store.delete(account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
