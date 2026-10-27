"""FastAPI dependencies. State is loaded once at startup and injected from ``app.state``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.accounts.auth import AuthContext, decode_access_token
from app.accounts.crypto import EnvelopeEncryptor
from app.accounts.db import session_scope
from app.accounts.models import UserRow
from app.accounts.service import AccountService, AuditService, BindingService, UserService
from app.core.errors import ConfigError
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.core.settings import Settings, get_settings
from app.orchestrator.persistence import RunStore
from app.projects.runs import InMemoryRunsQuery, RunsQuery, StoreRunsQuery
from app.projects.service import ProjectService
from app.providers.pool import ProviderPool

__all__ = [
    "AccountServiceDep",
    "AuthDep",
    "BindingServiceDep",
    "ConfigDep",
    "CurrentUserDep",
    "PoliciesDep",
    "ProjectServiceDep",
    "RubricDep",
    "RunsQueryDep",
    "SessionDep",
    "SettingsDep",
    "get_account_service",
    "get_auth_context",
    "get_binding_service",
    "get_current_user",
    "get_policies_dep",
    "get_pool",
    "get_project_service",
    "get_rubric_dep",
    "get_runs_query",
    "get_session",
    "get_settings_dep",
    "get_user_service",
]


def get_settings_dep(request: Request) -> Settings:
    """The settings this app instance was built with.

    Deliberately reads ``app.state.settings`` rather than the process-global cached
    singleton: tests, tools and future multi-tenant runs build an app with explicit
    settings, and an endpoint that silently used a different configuration would be
    the hardest kind of bug to find.
    """
    app_settings: Settings | None = getattr(request.app.state, "settings", None)
    return app_settings if app_settings is not None else get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_policies_dep(request: Request) -> Policies:
    """The loaded policies, or 503 when startup could not load them."""
    policies: Policies | None = getattr(request.app.state, "policies", None)
    if policies is None:
        raise _config_unavailable(request, "policies")
    return policies


def get_rubric_dep(request: Request) -> Rubric:
    """The loaded rubric, or 503 when startup could not load it."""
    rubric: Rubric | None = getattr(request.app.state, "rubric", None)
    if rubric is None:
        raise _config_unavailable(request, "rubric")
    return rubric


def _config_unavailable(request: Request, name: str) -> HTTPException:
    errors: dict[str, str] = getattr(request.app.state, "config_errors", {})
    detail = errors.get(name, f"{name} was not loaded at startup")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": ConfigError.code, "message": detail},
    )


PoliciesDep = Annotated[Policies, Depends(get_policies_dep)]
RubricDep = Annotated[Rubric, Depends(get_rubric_dep)]
ConfigDep = Annotated[tuple[Policies, Rubric], Depends(lambda: None)]


# --------------------------------------------------------------------------- database
def get_session(request: Request) -> Iterator[Session]:
    """One session per request; commit at the end, roll back on error."""
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------- auth
def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        from app.core.errors import AuthError

        raise AuthError("missing bearer token")
    return header[7:].strip()


def get_auth_context(request: Request) -> AuthContext:
    """The verified caller. 401 on anything missing, malformed or expired."""
    return decode_access_token(_bearer_token(request))


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]


def get_current_user(request: Request, session: SessionDep) -> UserRow:
    """The user row for the bearer token, or 401 when the account is gone."""
    context = get_auth_context(request)
    user = session.get(UserRow, context.user_id)
    if user is None:
        from app.core.errors import AuthError

        raise AuthError("token subject no longer exists")
    return user


CurrentUserDep = Annotated[UserRow, Depends(get_current_user)]


# --------------------------------------------------------------------------- services
def get_encryptor(request: Request) -> EnvelopeEncryptor | None:
    """The app's envelope encryptor, or None when APP_MASTER_KEY is not configured."""
    encryptor: EnvelopeEncryptor | None = getattr(request.app.state, "encryptor", None)
    return encryptor


def get_audit_service(session: SessionDep) -> AuditService:
    return AuditService(session)


def get_user_service(session: SessionDep) -> UserService:
    return UserService(session)


def get_account_service(request: Request, session: SessionDep) -> AccountService:
    return AccountService(session, get_encryptor(request))


def get_binding_service(session: SessionDep) -> BindingService:
    return BindingService(session)


# --------------------------------------------------------------------------- projects
def get_runs_query(request: Request) -> RunsQuery:
    """The projects -> runs read seam.

    Precedence: an explicit override on ``app.state.runs_query`` (tests, alternate
    stores), then the real ``RunStore`` built by the lifespan (``StoreRunsQuery``),
    then an empty in-memory fake so the projects API still answers honestly on an app
    built without a run store. Nothing here starts, mutates or blocks on a run.
    """
    override: RunsQuery | None = getattr(request.app.state, "runs_query", None)
    if override is not None:
        return override
    store: RunStore | None = getattr(request.app.state, "run_store", None)
    if store is None:  # pragma: no cover - lifespan always builds a store
        return InMemoryRunsQuery()
    cached: StoreRunsQuery | None = getattr(request.app.state, "store_runs_query", None)
    if cached is None or cached.store is not store:
        cached = StoreRunsQuery(store)
        request.app.state.store_runs_query = cached
    return cached


def get_project_service(request: Request, session: SessionDep) -> ProjectService:
    """Project CRUD bound to this request's session and the runs read seam."""
    return ProjectService(session, get_runs_query(request))


def get_pool(request: Request) -> ProviderPool:
    """The app's provider pool (call-time account resolution, breaker, usage)."""
    pool: ProviderPool | None = getattr(request.app.state, "pool", None)
    if pool is None:  # pragma: no cover - lifespan always builds one
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": ConfigError.code, "message": "provider pool not initialised"},
        )
    return pool


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
BindingServiceDep = Annotated[BindingService, Depends(get_binding_service)]
CurrentUser = CurrentUserDep
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
PoolDep = Annotated[ProviderPool, Depends(get_pool)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
RunsQueryDep = Annotated[RunsQuery, Depends(get_runs_query)]
