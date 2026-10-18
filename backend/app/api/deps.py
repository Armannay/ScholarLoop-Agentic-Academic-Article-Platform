"""FastAPI dependencies. State is loaded once at startup and injected from ``app.state``."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.accounts.store import AccountStore
from app.core.errors import ConfigError
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.core.settings import Settings, get_settings

__all__ = [
    "AccountStoreDep",
    "ConfigDep",
    "PoliciesDep",
    "RubricDep",
    "SettingsDep",
    "get_account_store_dep",
    "get_policies_dep",
    "get_rubric_dep",
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


def get_account_store_dep(request: Request) -> AccountStore:
    """The encrypted account store, or 503 when no store secret is configured.

    The store needs ``SCHOLARLOOP_SECRET_KEY``: without it there is no way to
    encrypt a provider key, so the account endpoints refuse to pretend otherwise.
    """
    store: AccountStore | None = getattr(request.app.state, "account_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "account_store_disabled",
                "message": (
                    "the account store is disabled: set SCHOLARLOOP_SECRET_KEY to enable it"
                ),
            },
        )
    return store


AccountStoreDep = Annotated[AccountStore, Depends(get_account_store_dep)]


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
