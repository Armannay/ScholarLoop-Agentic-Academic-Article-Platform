"""Model tier resolution: policies.yaml tiers + per-agent binding overrides.

The policy file maps *roles* to *tiers* (strong | mid | cheap). A binding maps
a *role* to a concrete ``model`` id. Tiers never name a model; bindings do.
Resolution is therefore:

1. If the role has an enabled :class:`app.contracts.binding.AgentBinding` with
   a non-empty ``model``, that model wins (per-agent override).
2. Otherwise the policy tier for the role is looked up and mapped through a
   tier-to-model table (defaults below, overridable per deployment).

``# VERIFY``: tier -> model ids change when vendors ship new models; the table
below is a documented default, not a price list. Deployments pass their own
``tier_models`` mapping (``ModelTier`` or string keys) instead of editing code.
"""

from __future__ import annotations

from typing import Any

from app.contracts.binding import AgentBinding
from app.contracts.enums import BindingRole, ModelTier
from app.core.policies import Policies

__all__ = [
    "DEFAULT_TIER_MODELS",
    "resolve_model",
    "resolve_tier",
    "tier_for_role",
]

# Default model per tier. Marked # VERIFY: vendors change ids without notice.
# These are the "known good" defaults used when no binding override exists.
DEFAULT_TIER_MODELS: dict[ModelTier, str] = {
    ModelTier.STRONG: "claude-opus-4-20250514",
    ModelTier.MID: "claude-sonnet-4-20250514",
    ModelTier.CHEAP: "claude-haiku-4-5-20251001",
}

# Backwards-compat alias some callers use.
TIER_DEFAULTS = DEFAULT_TIER_MODELS


def tier_for_role(role: BindingRole | str, policies: Policies) -> ModelTier:
    """Tier for ``role`` from the effective policies.

    Unknown roles fall back to ``mid`` (the policy loader documents the same
    rule) so a new role cannot silently get the expensive tier.
    """
    # BindingRole is a StrEnum (subclass of str) so test enum first.
    if isinstance(role, BindingRole):
        return policies.tier_for(role)
    if isinstance(role, str):
        try:
            role_enum = BindingRole(role)
        except ValueError:
            return ModelTier.MID
        return policies.tier_for(role_enum)
    # Exhaustive for type checker; runtime fallback to mid
    return ModelTier.MID  # type: ignore[unreachable]


def resolve_tier(role: BindingRole | str, policies: Policies) -> ModelTier:
    """Alias for :func:`tier_for_role`."""
    return tier_for_role(role, policies)


def resolve_model(
    role: BindingRole | str,
    policies: Policies,
    binding: AgentBinding | None = None,
    tier_models: dict[ModelTier, str] | dict[str, str] | None = None,
) -> str:
    """Resolve the model id for ``role``.

    Precedence:

    1. ``binding.model`` when ``binding`` is not ``None``, ``binding.enabled``
       is ``True`` and ``binding.model`` is non-blank.
    2. Tier mapping from ``tier_models`` (or :data:`DEFAULT_TIER_MODELS`).

    Args:
        role: binding role string or enum (planner, architect, ... compactor).
        policies: effective policies (supplies the tier per role).
        binding: optional per-role binding; its ``model`` overrides the tier.
        tier_models: ``ModelTier -> model id`` mapping. When ``None`` the
            shipped defaults are used. String keys are coerced to
            :class:`ModelTier` for YAML convenience.

    Returns:
        The model id to pass to the provider.

    Raises:
        ValueError: when the tier has no entry in the resolved table.

    Example::

        policies = load_policies(Path("config/policies.yaml"))
        planner_model = resolve_model("planner", policies)  # strong-tier default
        writer_model = resolve_model("writer", policies, binding=writer_binding)  # override
    """
    # 1. Binding override
    if binding is not None and binding.enabled and binding.model.strip():
        return binding.model.strip()

    # 2. Tier -> model
    tier = tier_for_role(role, policies)

    table: dict[ModelTier, str]
    if tier_models is None:
        table = DEFAULT_TIER_MODELS
    else:
        table = {}
        for key, value in tier_models.items():
            if isinstance(key, ModelTier):
                table[key] = str(value)
            elif isinstance(key, str):
                try:
                    table[ModelTier(key)] = str(value)
                except ValueError as exc:
                    raise ValueError(f"unknown tier {key!r} in tier_models") from exc
            else:
                raise ValueError(
                    f"tier_models key must be str or ModelTier, got {type(key).__name__}"
                )

    model = table.get(tier)
    if not model:
        raise ValueError(
            f"no model configured for tier {tier.value!r} (role {role!r}); fill tier_models"
        )
    return model


def resolve(
    role: BindingRole | str,
    policies: Policies,
    binding: AgentBinding | None = None,
    tier_models: dict[ModelTier, str] | dict[str, str] | None = None,
) -> str:
    """Alias for :func:`resolve_model` (shorter call sites)."""
    return resolve_model(role, policies, binding=binding, tier_models=tier_models)


def model_for_role(
    role: BindingRole | str,
    policies: Policies,
    binding: AgentBinding | None = None,
    tier_models: dict[Any, str] | None = None,
) -> str:
    """Alias for :func:`resolve_model` (historic name)."""
    return resolve_model(role, policies, binding=binding, tier_models=tier_models)


def describe_resolution(
    role: BindingRole | str,
    policies: Policies,
    binding: AgentBinding | None = None,
    tier_models: dict[ModelTier, str] | dict[str, str] | None = None,
) -> dict[str, Any]:
    """Explain *why* a model was chosen (for logs and :mod:`telemetry`)."""
    tier = tier_for_role(role, policies)
    if binding is not None and binding.enabled and binding.model.strip():
        return {
            "role": str(role.value) if isinstance(role, BindingRole) else str(role),
            "tier": tier.value,
            "model": binding.model.strip(),
            "source": "binding",
            "binding_id": getattr(binding, "id", None),
        }
    model = resolve_model(role, policies, binding=None, tier_models=tier_models)
    return {
        "role": str(role.value) if isinstance(role, BindingRole) else str(role),
        "tier": tier.value,
        "model": model,
        "source": "tier",
    }
