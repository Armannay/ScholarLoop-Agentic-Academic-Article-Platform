"""Provider account CRUD + test endpoint. Keys are write-only, ownership enforced."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from app.accounts.schemas import (
    AccountCreate,
    AccountPatch,
    AccountTestResult,
    AccountView,
    DeleteResult,
)
from app.api.deps import AccountServiceDep, CurrentUserDep, PoolDep, SessionDep

__all__ = ["router"]

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountView], summary="List my provider accounts")
def list_accounts(user: CurrentUserDep, accounts: AccountServiceDep) -> list[AccountView]:
    return accounts.list_for_owner(user.id)


@router.post(
    "",
    response_model=AccountView,
    status_code=status.HTTP_201_CREATED,
    summary="Add a provider account (key stored encrypted, never returned)",
)
def create_account(
    payload: AccountCreate,
    user: CurrentUserDep,
    accounts: AccountServiceDep,
    session: SessionDep,
) -> AccountView:
    view = accounts.create(user.id, payload)
    session.commit()
    return view


@router.get("/{account_id}", response_model=AccountView, summary="One account (no key material)")
def get_account(account_id: str, user: CurrentUserDep, accounts: AccountServiceDep) -> AccountView:
    return accounts.get(user.id, account_id)


@router.patch(
    "/{account_id}", response_model=AccountView, summary="Rotate key / relabel / budget / status"
)
def patch_account(
    account_id: str,
    payload: AccountPatch,
    user: CurrentUserDep,
    accounts: AccountServiceDep,
    session: SessionDep,
) -> AccountView:
    view = accounts.patch(user.id, account_id, payload)
    session.commit()
    return view


@router.delete(
    "/{account_id}",
    response_model=DeleteResult,
    summary="Delete an account (protected against orphaning enabled roles)",
)
def delete_account(
    account_id: str,
    user: CurrentUserDep,
    accounts: AccountServiceDep,
    pool: PoolDep,
    session: SessionDep,
    response: Response,
    replacement_account_id: str | None = Query(default=None),
    disable_roles: bool = Query(default=False),
) -> DeleteResult:
    result = accounts.delete(
        user.id,
        account_id,
        replacement_account_id=replacement_account_id,
        disable_roles=disable_roles,
    )
    session.commit()
    # Anything in flight keeps its provider; new calls see the account is gone.
    pool.evict(account_id)
    response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/{account_id}/test",
    response_model=AccountTestResult,
    summary="Probe the credential with one tiny call (no key echoed)",
)
async def test_account(
    account_id: str, user: CurrentUserDep, accounts: AccountServiceDep, session: SessionDep
) -> AccountTestResult:
    result = await accounts.test(user.id, account_id)
    session.commit()
    return result
