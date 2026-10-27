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
from sqlalchemy import func, select

from app import CONTRACT_VERSION, __version__
from app.accounts.crypto import (
    EnvelopeEncryptor,
    MasterKey,
    configure_master_key,
    get_master_key,
    install_key_redaction,
)
from app.accounts.db import create_session_factory, init_db, make_engine
from app.accounts.models import AccountRow
from app.accounts.service import SqlAccountResolver, SqlUsageSink
from app.api.routers import accounts as accounts_router
from app.api.routers import agents as agents_router
from app.api.routers import auth as auth_router
from app.api.routers import config as config_router
from app.api.routers import health as health_router
from app.api.routers import projects as projects_router
from app.api.routers import runs as runs_router
from app.core.errors import ConfigError, ScholarLoopError
from app.core.ids import new_ulid
from app.core.logging import configure_logging, get_logger
from app.core.policies import load_policies
from app.core.redaction import redact
from app.core.rubric import load_rubric
from app.core.settings import Settings, get_settings
from app.providers.errors import ProviderError
from app.providers.pool import ProviderPool

__all__ = ["app", "create_app"]

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load config into app state; record failures instead of crashing the process."""
    settings: Settings = app.state.settings
    configure_logging(level=settings.log_level)
    install_key_redaction()
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

    # Account store: engine + schema + the provider pool that resolves keys per call.
    engine = make_engine(settings.database_url)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    raw_master_key = settings.master_key or settings.secret_key
    try:
        # Settings (including .env) is the first source; a bare environment variable
        # still works because configure_master_key(None) leaves lookup to MasterKey.
        configure_master_key(
            MasterKey.from_string(raw_master_key.get_secret_value())
            if raw_master_key is not None
            else None
        )
        app.state.encryptor = EnvelopeEncryptor(get_master_key())
    except ConfigError as exc:
        # Boot without a master key: health still answers, account ops say why.
        # health reports this as providers.account_store_ready=false (not a config error).
        app.state.encryptor = None
        logger.warning("account store sealed: %s", exc.message)
    app.state.pool = ProviderPool(
        SqlAccountResolver(app.state.session_factory, app.state.encryptor),
        usage_sink=SqlUsageSink(app.state.session_factory),
    )
    with app.state.session_factory() as session:
        app.state.accounts_configured = int(
            session.scalar(select(func.count()).select_from(AccountRow)) or 0
        )
    # Orchestrator (Step 9): run records persist to disk (crash resume);
    # per-owner RunManagers are built lazily by the runs router.
    from app.orchestrator.persistence import FileRunStore

    app.state.run_store = FileRunStore(settings.runs_path)
    app.state.run_managers = {}
    logger.info("account store ready", extra={"accounts": app.state.accounts_configured})
    try:
        yield
    finally:
        configure_master_key(None)
        await app.state.pool.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests pass explicit settings; production uses the env."""
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="ScholarLoop",
        version=__version__,
        description=(
            "Agentic academic article platform: a deterministic orchestrator drives a "
            "planning-only LLM and five executing agents. Exposes health, configuration, "
            "contract schemas, accounts/bindings, and the run API (Step 9)."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved
    app.state.config_errors = {}

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
    async def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """422 without ever echoing the submitted value.

        FastAPI's default body includes ``input`` and ``ctx`` for every error, which
        turns a rejected oversized ``api_key`` into a response carrying the key. We
        keep the actionable location and message and drop the value entirely.
        """
        errors: list[dict[str, str]] = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or "<body>"
            message = str(item.get("msg", "invalid"))
            if location.endswith("api_key"):
                message = "invalid api_key"
            errors.append(
                {
                    "loc": location,
                    "msg": redact(message),
                    "type": str(item.get("type", "value_error")),
                }
            )
        if any(error["loc"].endswith("api_key") for error in errors):
            summary = "invalid api_key"
        else:
            summary = "; ".join(f"{error['loc']}: {error['msg']}" for error in errors[:4])
            if len(errors) > 4:
                summary += f" (+{len(errors) - 4} more)"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": redact(summary),
                    "context": {"errors": errors},
                }
            },
        )

    app.include_router(health_router.router)
    app.include_router(config_router.router)
    app.include_router(auth_router.router)
    app.include_router(accounts_router.router)
    app.include_router(agents_router.router)
    app.include_router(projects_router.router)
    app.include_router(runs_router.router)

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
