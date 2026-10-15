"""Typed policy loader for ``config/policies.yaml`` with project-level deep merge.

File format (documented once, here, and mirrored in the YAML's own comments)::

    schema_version: 1
    policies:            # the base policy block - any subset of the schema below
      run_token_cap: 800000
      ...
    projects:            # optional named override blocks, deep-merged over `policies`
      grant-proposal:
        q_min: 80
        plateau:
          min_gain: 2.0

Merge precedence, lowest to highest:

1. code defaults (:class:`Policies`)
2. the base file's ``policies`` block
3. the base file's ``projects.<project>`` block
4. an explicit overrides file (``SCHOLARLOOP_PROJECT_OVERRIDES_PATH``), ``policies``
5. that overrides file's ``projects.<project>`` block

Merging is *deep*: ``plateau: {min_gain: 2.0}`` changes one nested key and leaves
``window`` alone. Unknown keys anywhere are a hard error (``ConfigError``) - a
mistyped policy that silently keeps the default is worse than a broken build.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import Field, ValidationError, model_validator

from app.contracts.common import Contract, VersionedContract
from app.contracts.enums import BindingRole, ModelTier
from app.core.errors import ConfigError, PolicyError, format_validation_errors

__all__ = [
    "DEFAULT_POLICIES",
    "DiminishingPolicy",
    "ModelTierPolicy",
    "OscillationPolicy",
    "PlateauPolicy",
    "Policies",
    "RegressionPolicy",
    "deep_merge",
    "load_policies",
]

POLICY_SCHEMA_VERSION: Final = 1


class PlateauPolicy(Contract):
    """Stop improving-by-inches: no meaningful gain over ``window`` iterations."""

    window: int = Field(default=3, ge=2, le=10)
    min_gain: float = Field(
        default=1.0, ge=0.0, le=50.0, description="Q points that count as 'still improving'."
    )


class RegressionPolicy(Contract):
    """Q dropped by ``delta`` or more inside ``window`` iterations."""

    delta: float = Field(default=3.0, ge=0.0, le=50.0)
    window: int = Field(default=2, ge=1, le=10)


class OscillationPolicy(Contract):
    """Q alternates up/down for ``window`` iterations: the agents are not converging."""

    window: int = Field(default=4, ge=2, le=10)


class DiminishingPolicy(Contract):
    """Gain per 10k tokens falls below ``min_gain_per_10k`` for ``window`` iterations."""

    min_gain_per_10k: float = Field(default=0.2, ge=0.0, le=10.0)
    window: int = Field(default=2, ge=1, le=10)


class ModelTierPolicy(Contract):
    """Model tier per LLM role. The tier is resolved to a model id by the binding."""

    planner: ModelTier = ModelTier.STRONG
    architect: ModelTier = ModelTier.MID
    researcher: ModelTier = ModelTier.MID
    writer: ModelTier = ModelTier.MID
    reviewer: ModelTier = ModelTier.STRONG
    validator: ModelTier = ModelTier.CHEAP
    compactor: ModelTier = ModelTier.CHEAP

    def tier_for(self, role: BindingRole) -> ModelTier:
        """Tier for a binding role, falling back to ``mid`` for unknown roles."""
        mapping = {
            BindingRole.PLANNER: self.planner,
            BindingRole.ARCHITECT: self.architect,
            BindingRole.RESEARCHER: self.researcher,
            BindingRole.WRITER: self.writer,
            BindingRole.REVIEWER: self.reviewer,
            BindingRole.VALIDATOR: self.validator,
            BindingRole.COMPACTOR: self.compactor,
        }
        return mapping.get(role, ModelTier.MID)


class Policies(VersionedContract):
    """Every knob the deterministic loop obeys. Defaults are the shipped defaults."""

    run_token_cap: int = Field(default=800_000, ge=10_000, le=100_000_000)
    iteration_token_cap: int = Field(default=120_000, ge=1_000, le=10_000_000)
    call_input_cap: int = Field(
        default=24_000, ge=500, le=1_000_000, description="Prompt ceiling for any single call."
    )
    max_iterations: int = Field(default=12, ge=1, le=200)
    max_parallel: int = Field(default=3, ge=1, le=16)
    digest_max_tokens: int = Field(
        default=2_000, ge=200, le=20_000, description="Hard ceiling for the claude.md digest."
    )
    recent_window: int = Field(default=3, ge=1, le=20, description="Ledger entries kept verbatim.")
    compact_trigger: float = Field(
        default=0.8, gt=0.0, le=1.0, description="Compact the digest at this fraction of its cap."
    )
    q_min: int = Field(default=70, ge=0, le=100, description="Q below this is never 'done'.")
    plateau: PlateauPolicy = Field(default_factory=PlateauPolicy)
    regression: RegressionPolicy = Field(default_factory=RegressionPolicy)
    oscillation: OscillationPolicy = Field(default_factory=OscillationPolicy)
    diminishing: DiminishingPolicy = Field(default_factory=DiminishingPolicy)
    issue_churn: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Issues opened per issue closed above this ratio means churn.",
    )
    error_streak: int = Field(
        default=3, ge=1, le=20, description="Consecutive failed calls tolerated before STOP_ERRORS."
    )
    model_tiers: ModelTierPolicy = Field(default_factory=ModelTierPolicy)

    @model_validator(mode="after")
    def _budgets_are_nested_consistently(self) -> Policies:
        if self.iteration_token_cap > self.run_token_cap:
            raise ValueError(
                "iteration_token_cap must not exceed run_token_cap "
                f"({self.iteration_token_cap} > {self.run_token_cap})"
            )
        if self.call_input_cap >= self.iteration_token_cap:
            raise ValueError(
                "call_input_cap must stay below iteration_token_cap "
                f"({self.call_input_cap} >= {self.iteration_token_cap})"
            )
        if self.digest_max_tokens >= self.call_input_cap:
            raise ValueError(
                "digest_max_tokens must stay below call_input_cap "
                f"({self.digest_max_tokens} >= {self.call_input_cap})"
            )
        return self

    @property
    def digest_budget_bytes(self) -> int:
        """Rough byte budget for the digest: ~4 characters per token."""
        return self.digest_max_tokens * 4

    @property
    def compact_at_tokens(self) -> int:
        """Digest size (tokens) at which the compactor is invoked."""
        return int(self.digest_max_tokens * self.compact_trigger)

    def tier_for(self, role: BindingRole) -> ModelTier:
        """Model tier for a role."""
        return self.model_tiers.tier_for(role)


#: The shipped defaults, instantiated once: `make schemas`, tests and the API all
#: read this instead of re-declaring numbers.
DEFAULT_POLICIES: Final[Policies] = Policies()


def _require_mapping(value: Any, *, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def load_yaml_mapping(path: Path, *, required: bool = True) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping at the top level."""
    if not path.exists():
        if required:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    return _require_mapping(raw, where=str(path))


def deep_merge(
    base: Mapping[str, Any], override: Mapping[str, Any], *, where: str
) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``, rejecting unknown keys.

    ``where`` is a dotted path used in error messages, so a bad override names the
    exact key rather than just the file.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(merged[key], value, where=f"{where}.{key}")
        else:
            if key not in merged:
                known = ", ".join(sorted(merged))
                raise ConfigError(f"unknown policy key '{where}.{key}'; known keys here: {known}")
            merged[key] = value
    return merged


def _split_policy_document(
    document: Mapping[str, Any], *, where: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a policy document into ``(policies_block, projects_block)``."""
    schema_version = document.get("schema_version", POLICY_SCHEMA_VERSION)
    if schema_version != POLICY_SCHEMA_VERSION:
        raise ConfigError(
            f"{where}: schema_version must be {POLICY_SCHEMA_VERSION}, got {schema_version!r}"
        )
    policies_block = _require_mapping(document.get("policies"), where=f"{where}.policies")
    projects_block = _require_mapping(document.get("projects"), where=f"{where}.projects")
    unknown = set(document) - {"schema_version", "policies", "projects"}
    if unknown:
        raise ConfigError(
            f"{where}: unexpected top-level keys {sorted(unknown)}; "
            "allowed: schema_version, policies, projects"
        )
    return policies_block, projects_block


def _project_block(
    projects: Mapping[str, Any], project: str | None, *, where: str
) -> dict[str, Any] | None:
    """The ``projects.<name>`` block from one document, or None when it has none.

    Absence is not an error here: a project may be defined in the overrides file while
    the base file defines no projects at all. :func:`load_policies` raises only after
    checking every document, so a typo still cannot pass silently.
    """
    if project is None or project not in projects:
        return None
    return _require_mapping(projects[project], where=f"{where}.projects.{project}")


def load_policies(
    path: Path,
    *,
    overrides_path: Path | None = None,
    project: str | None = None,
    base: Policies | None = None,
) -> Policies:
    """Load, deep-merge and validate the policy set.

    Args:
        path: the base file (``config/policies.yaml``).
        overrides_path: optional second file with the same shape; wins over ``path``.
        project: select a named ``projects:`` block in either file.
        base: start from these policies instead of the code defaults (used by tests).

    Raises:
        ConfigError: file missing, unreadable, not a mapping, or an unknown key.
        PolicyError: values are present and well-formed YAML but fail validation.
    """
    base_policies = base if base is not None else DEFAULT_POLICIES

    document = load_yaml_mapping(path)
    policies_block, projects_block = _split_policy_document(document, where=str(path))
    merged = deep_merge(
        base_policies.model_dump(mode="json"),
        policies_block,
        where="policies",
    )

    known_projects: dict[str, str] = {name: str(path) for name in projects_block}
    project_block = _project_block(projects_block, project, where=str(path))
    if project_block:
        merged = deep_merge(merged, project_block, where=f"policies.projects.{project}")

    if overrides_path is not None:
        override_document = load_yaml_mapping(overrides_path, required=False)
        override_policies, override_projects = _split_policy_document(
            override_document, where=str(overrides_path)
        )
        if override_policies:
            merged = deep_merge(merged, override_policies, where="policies")
        known_projects.update({name: str(overrides_path) for name in override_projects})
        override_project_block = _project_block(
            override_projects, project, where=str(overrides_path)
        )
        if override_project_block:
            merged = deep_merge(
                merged,
                override_project_block,
                where=f"policies.projects.{project}",
            )

    if project is not None and project not in known_projects:
        known = ", ".join(sorted(known_projects)) or "(none defined)"
        raise ConfigError(
            f"no override block for project {project!r} in {known_projects or 'any policy file'}; "
            f"known projects: {known}"
        )

    try:
        return Policies.model_validate(merged)
    except ValidationError as exc:
        sources = str(path) + (f" and {overrides_path}" if overrides_path else "")
        raise PolicyError(
            f"invalid policies after merging {sources}"
            + (f" for project {project!r}" if project else "")
            + f" [{format_validation_errors(exc)}]",
            errors=exc.errors(include_url=False),
        ) from exc
