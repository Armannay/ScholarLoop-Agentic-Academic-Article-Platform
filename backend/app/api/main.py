"""FastAPI application factory.

Design notes that later steps depend on:

* config is loaded **once** at startup and injected, so a request never re-reads YAML;
* a config failure degrades the app instead of refusing to boot: ``/api/health``
  reports ``degraded`` and ``/api/config`` answers 503 with the reason. A dev server
  that will not start because of one bad key is worse than a loud error page - but a
  production run must never silently continue on defaults, hence the honest status;
* no provider is constructed here. Accounts are resolved at call time, and the only
  implemented provider is the mock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import CONTRACT_VERSION, __version__
from app.accounts.crypto import SecretBox, SecretBoxError
from app.accounts.store import InMemoryAccountStore
from app.api.routers import accounts as accounts_router
from app.api.routers import config as config_router
from app.api.routers import health as health_router
from app.core.errors import ConfigError, ScholarLoopError
from app.core.ids import new_ulid
from app.core.logging import configure_logging, get_logger
from app.core.policies import load_policies
from app.core.rubric import load_rubric
from app.core.settings import Settings, get_settings
from app.providers.errors import ProviderError

__all__ = ["app", "create_app"]

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load config into app state; record failures instead of crashing the process."""
    settings: Settings = app.state.settings
    configure_logging(level=settings.log_level)
    app.state.config_errors = {}

    overrides = (
        settings.resolve(settings.project_overrides_path)
        if settings.project_overrides_path
        else None
    )

    try:
        app.state.policies = load_policies(
            settings.policies_file,
            overrides_path=overrides,
            project=settings.project_name,
        )
        logger.info(
            "policies loaded",
            extra={
                "run_token_cap": app.state.policies.run_token_cap,
                "max_iterations": app.state.policies.max_iterations,
                "project": settings.project_name,
            },
        )
    except ConfigError as exc:
        app.state.policies = None
        app.state.config_errors["policies"] = exc.message
        logger.error("policies failed to load: %s", exc.message)

    try:
        app.state.rubric = load_rubric(settings.rubric_file)
        logger.info("rubric loaded", extra={"criteria": len(app.state.rubric.criteria)})
    except ConfigError as exc:
        app.state.rubric = None
        app.state.config_errors["rubric"] = exc.message
        logger.error("rubric failed to load: %s", exc.message)

    # Accounts live in the encrypted store; it exists only when a store secret is
    # configured, because a key cannot be encrypted without one.
    app.state.account_store = None
    if settings.secret_key is not None:
        try:
            app.state.account_store = InMemoryAccountStore(SecretBox(settings.secret_key))
        except SecretBoxError as exc:
            app.state.config_errors["account_store"] = exc.message
            logger.error("account store failed to start: %s", exc.message)

    store = app.state.account_store
    app.state.accounts_configured = len(store.list_accounts()) if store is not None else 0
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests pass explicit settings; production uses the env."""
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="ScholarLoop",
        version=__version__,
        description=(
            "Agentic academic article platform: a deterministic orchestrator drives a "
            "planning-only LLM and five executing agents. This build exposes health, "
            "configuration and contract schemas; the run endpoints arrive in later steps."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved
    app.state.config_errors = {}
    app.state.account_store = None
    app.state.accounts_configured = 0

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Tag every response with a request id, so a log line can be traced back."""
        request_id = request.headers.get("x-request-id") or new_ulid()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-contract-version"] = str(CONTRACT_VERSION)
        return response

    @app.exception_handler(ScholarLoopError)
    async def scholarloop_error_handler(_: Request, exc: ScholarLoopError) -> JSONResponse:
        """One shape for every expected failure: ``{code, message, context}``."""
        status = exc.http_status
        if isinstance(exc, ProviderError) and exc.status_code == 429:
            status = 429
        return JSONResponse(status_code=status, content={"error": exc.to_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """A 422 body that never echoes the request.

        FastAPI's default handler copies the offending value into the response.
        A request body may carry a provider key, so the value is dropped here and
        only the location, the message and the error type survive (HARD RULE 2).
        """
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "invalid value")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "the request is not valid",
                    "context": {"errors": errors},
                }
            },
        )

    app.include_router(health_router.router)
    app.include_router(config_router.router)
    app.include_router(accounts_router.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        """Tiny landing payload so a bare visit is not a 404."""
        return {
            "service": "scholarloop",
            "version": __version__,
            "docs": "/docs",
            "health": "/api/health",
        }

    return app


app = create_app()
