"""Health and version endpoints. Cheap, always available, secret-free."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import CONTRACT_VERSION, __version__
from app.api.deps import SettingsDep
from app.api.schemas import HealthResponse, ProviderStatus
from app.providers.factory import SUPPORTED_PROVIDERS

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness and readiness")
def health(request: Request, settings: SettingsDep) -> HealthResponse:
    """Report liveness along with whether config loaded and which providers exist."""
    config_errors: dict[str, str] = getattr(request.app.state, "config_errors", {})
    policies_loaded = getattr(request.app.state, "policies", None) is not None
    rubric_loaded = getattr(request.app.state, "rubric", None) is not None

    live_adapters = sorted(
        provider.value for provider in SUPPORTED_PROVIDERS if provider.value != "mock"
    )
    store_ready = getattr(request.app.state, "encryptor", None) is not None
    providers = ProviderStatus(
        mock=True,
        live_adapters_implemented=live_adapters,
        accounts_configured=int(getattr(request.app.state, "accounts_configured", 0)),
        live_providers_enabled=store_ready,
        account_store_ready=store_ready,
    )

    return HealthResponse(
        status="ok" if policies_loaded and rubric_loaded else "degraded",
        version=__version__,
        contract_version=CONTRACT_VERSION,
        env=settings.env,
        policies_loaded=policies_loaded,
        rubric_loaded=rubric_loaded,
        providers=providers,
        config_errors=config_errors,
    )


@router.get("/version", summary="Versions")
def version(settings: SettingsDep) -> dict[str, str | int]:
    """Application, contract and environment identifiers in one call."""
    return {
        "app_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "env": settings.env,
        "provider_default": "mock",
    }
