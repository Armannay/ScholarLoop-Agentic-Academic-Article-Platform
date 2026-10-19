"""Register, login, whoami. Minimal auth for the accounts API (argon2 + JWT)."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.accounts.auth import create_access_token
from app.accounts.models import UserRow
from app.accounts.schemas import LoginRequest, RegisterRequest, TokenResponse, UserView
from app.api.deps import CurrentUserDep, SessionDep, UserServiceDep

__all__ = ["router"]

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_view(user: UserRow) -> UserView:
    return UserView(id=user.id, email=user.email, created_at=user.created_at)


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and receive a bearer token",
)
def register(payload: RegisterRequest, users: UserServiceDep, session: SessionDep) -> TokenResponse:
    user = users.register(payload.email, payload.password)
    session.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.email),
        user=_user_view(user),
    )


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a token")
def login(payload: LoginRequest, users: UserServiceDep, session: SessionDep) -> TokenResponse:
    user = users.authenticate(payload.email, payload.password)
    session.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.email),
        user=_user_view(user),
    )


@router.get("/me", response_model=UserView, summary="The authenticated user")
def me(user: CurrentUserDep) -> UserView:
    return _user_view(user)
