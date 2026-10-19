"""Configuration introspection: effective policies, rubric summary, contract schemas.

These endpoints exist so the UI (and a human debugging a run) can see *exactly* which
numbers the loop is obeying, including any project override that was merged in.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, status

from app import CONTRACT_VERSION
from app.api.deps import PoliciesDep, RubricDep, SettingsDep
from app.api.schemas import (
    ConfigResponse,
    ContractListResponse,
    RubricSummary,
    ToolCatalogResponse,
    ToolSpecResponse,
)
from app.contracts.enums import AgentRole, ArticleType
from app.contracts.registry import CONTRACT_NAMES, get_contract, json_schema_for
from app.core.rubric import Rubric
from app.tools.base import DEFAULT_ALLOWED_TOOLS_BY_ROLE
from app.tools.catalog import catalog

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["config"])


def summarise_rubric(rubric: Rubric) -> RubricSummary:
    """Compact, JSON-friendly view of the rubric (required sections, gates, weights)."""
    default_spec = rubric.spec_for(ArticleType.RESEARCH)
    return RubricSummary(
        article_types={
            article_type.value: rubric.spec_for(article_type).required_ids
            for article_type in ArticleType
        },
        gates=list(rubric.gate_names),
        criteria_weights=rubric.weights(),
        weights_total=round(sum(rubric.weights().values()), 6),
        abstract_word_range={
            "min": default_spec.abstract.min_words,
            "max": default_spec.abstract.max_words,
        },
    )


@router.get("/config", response_model=ConfigResponse, summary="Effective configuration")
def config(settings: SettingsDep, policies: PoliciesDep, rubric: RubricDep) -> ConfigResponse:
    """Policies after merging every override, plus the rubric summary."""
    return ConfigResponse(
        contract_version=CONTRACT_VERSION,
        policies_path=str(settings.policies_file),
        overrides_path=(
            str(settings.resolve(settings.project_overrides_path))
            if settings.project_overrides_path
            else None
        ),
        project=settings.project_name,
        policies=policies.model_dump(mode="json"),
        rubric=summarise_rubric(rubric),
    )


@router.get("/contracts", response_model=ContractListResponse, summary="Contract registry")
def contracts() -> ContractListResponse:
    """Names of every CONTRACTS v1 model and where to fetch its JSON Schema."""
    return ContractListResponse(
        contract_version=CONTRACT_VERSION,
        names=list(CONTRACT_NAMES),
        schema_url_template="/api/contracts/{name}/schema",
    )


@router.get("/contracts/{name}/schema", summary="JSON Schema for one contract")
def contract_schema(
    name: str = Path(description="Contract name, e.g. AgentResult"),
) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a contract, as exported to ``docs/schemas``."""
    try:
        return json_schema_for(name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc), "context": {"name": name}},
        ) from exc


@router.get("/contracts/{name}/model.json", include_in_schema=False)
def contract_model_json(name: str) -> dict[str, Any]:
    """The model's own JSON schema (without the wrapper ``$id``/``title``)."""
    return get_contract(name).model_json_schema(mode="validation")


@router.get("/tools", response_model=ToolCatalogResponse, summary="Root tool catalog")
def tools() -> ToolCatalogResponse:
    """Every root tool, the roles allowed to call it, and what it needs to be wired.

    The registry resolves availability per run; this endpoint reports the *policy*, so
    an operator can see why a role has no tools without starting a run.
    """
    return ToolCatalogResponse(
        tools=[ToolSpecResponse.model_validate(row) for row in catalog()],
        role_allowlist={
            role.value: sorted(DEFAULT_ALLOWED_TOOLS_BY_ROLE[role]) for role in AgentRole
        },
    )
