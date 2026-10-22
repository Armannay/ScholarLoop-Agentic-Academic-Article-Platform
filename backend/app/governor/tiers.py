"""Model tiers: policies decide the *class* of model, the binding decides the id.

``config/policies.yaml`` names a tier per role (``planner: strong``,
``validator: cheap`` ...). A tier is a cost/latency class, never a model id: vendor ids
change, tiers do not. :class:`TierResolver` turns ``(role, provider, binding)`` into one
concrete model id plus the caps the call must respect:

1. **A binding's model wins.** A user who pinned ``claude-sonnet-4-5`` for the writer
   gets exactly that; the tier is still reported, because telemetry compares spend per
   tier, not per model string.
2. **Otherwise the provider's table decides.** :data:`DEFAULT_MODELS` maps
   ``(provider, tier) -> model id``. These ids are third-party details, so the table is
   marked ``# VERIFY`` and is the only place a vendor id is written down.
3. **No provider and no binding is an error**, not a guess: a call that cannot name its
   model would be billed to nobody's account.

Caps come from the binding when present (``max_input_tokens`` / ``max_output_tokens``)
and are ``None`` otherwise, which lets the governor fall back to ``Policies``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountProvider, BindingRole, ModelTier
from app.core.errors import ConfigError
from app.core.policies import DEFAULT_POLICIES, Policies
from app.providers.mock import DEFAULT_MODEL as MOCK_DEFAULT_MODEL

__all__ = [
    "DEFAULT_MODELS",
    "ModelChoice",
    "ModelSource",
    "TierResolver",
    "as_binding_role",
]

#: Role -> tier defaults live in policies; this is only the tier -> model id table.
# VERIFY: vendor model ids and their tier mapping change without notice. Check these
# against the provider's current model list before relying on a cost estimate.
DEFAULT_MODELS: Final[dict[AccountProvider, dict[ModelTier, str]]] = {
    AccountProvider.ANTHROPIC: {
        ModelTier.STRONG: "claude-opus-4",
        ModelTier.MID: "claude-sonnet-4",
        ModelTier.CHEAP: "claude-haiku-4",
    },
    AccountProvider.OPENAI_COMPATIBLE: {
        ModelTier.STRONG: "gpt-4.1",
        ModelTier.MID: "gpt-4.1-mini",
        ModelTier.CHEAP: "gpt-4o-mini",
    },
    AccountProvider.MOCK: {
        ModelTier.STRONG: "mock-strong-1",
        ModelTier.MID: MOCK_DEFAULT_MODEL,
        ModelTier.CHEAP: "mock-cheap-1",
    },
}


class ModelSource(StrEnum):
    """Why the resolver picked the model it picked."""

    BINDING_OVERRIDE = "binding_override"
    PROVIDER_DEFAULT = "provider_default"


@dataclass(frozen=True)
class ModelChoice:
    """The concrete model for one role, with the tier and caps behind the choice."""

    role: BindingRole
    tier: ModelTier
    model: str
    source: ModelSource
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")

    @property
    def is_binding_override(self) -> bool:
        """True when a user binding pinned this model instead of the tier table."""
        return self.source is ModelSource.BINDING_OVERRIDE


def as_binding_role(role: BindingRole | str) -> BindingRole:
    """Coerce a role string (``planner``, ``writer`` ...) to :class:`BindingRole`.

    Raises:
        ConfigError: the role is not one of the seven binding roles. The message lists
            them, because a typo in a binding should name the alternatives.
    """
    if isinstance(role, BindingRole):
        return role
    try:
        return BindingRole(role)
    except ValueError as exc:
        known = ", ".join(item.value for item in BindingRole)
        raise ConfigError(
            f"unknown role {role!r} for model tier resolution; known roles: {known}", role=role
        ) from exc


class TierResolver:
    """Resolves roles to tiers to concrete model ids.

    Args:
        policies: the policy set that names the tier per role.
        models: override the tier->model table (tests, new vendors, private gateways).
    """

    def __init__(
        self,
        policies: Policies = DEFAULT_POLICIES,
        *,
        models: Mapping[AccountProvider, Mapping[ModelTier, str]] | None = None,
    ) -> None:
        self._policies = policies
        self._models: dict[AccountProvider, dict[ModelTier, str]] = {
            provider: dict(table) for provider, table in DEFAULT_MODELS.items()
        }
        for provider, table in (models or {}).items():
            self._models[provider] = dict(table)

    @property
    def policies(self) -> Policies:
        return self._policies

    def tier_for(self, role: BindingRole | str) -> ModelTier:
        """Tier configured for ``role`` (``planner`` strong, ``validator`` cheap ...)."""
        return self._policies.tier_for(as_binding_role(role))

    def model_for(self, provider: AccountProvider, tier: ModelTier) -> str:
        """Concrete model id for a provider and tier.

        Raises:
            ConfigError: the provider has no entry for the tier - adding a provider
                without deciding its tier models is a configuration bug.
        """
        table = self._models.get(provider)
        if table is None:
            known = ", ".join(item.value for item in self._models)
            raise ConfigError(
                f"no model table for provider {provider.value!r}; known providers: {known}",
                provider=provider.value,
            )
        model = table.get(tier)
        if not model:
            raise ConfigError(
                f"no {tier.value} model configured for provider {provider.value!r}",
                provider=provider.value,
                tier=tier.value,
            )
        return model

    def resolve(
        self,
        role: BindingRole | str,
        *,
        binding: AgentBinding | None = None,
        provider: AccountProvider | str | None = None,
    ) -> ModelChoice:
        """Resolve one role to a model, preferring the binding over the tier table.

        Args:
            role: planner, architect, researcher, writer, reviewer, validator, compactor.
            binding: the user's binding for this role, when one exists.
            provider: the provider of the account that will serve the call. Required
                unless ``binding`` pins a model.

        Raises:
            ValueError: ``binding`` is for a different role.
            ConfigError: no model can be named (no binding and no provider).
        """
        resolved_role = as_binding_role(role)
        tier = self.tier_for(resolved_role)
        if binding is not None:
            if binding.role is not resolved_role:
                raise ValueError(
                    f"binding is for role {binding.role.value!r}, not {resolved_role.value!r}"
                )
            if not binding.enabled:
                # A disabled binding is not a model preference: resolving it would send
                # the call to a configuration the user has switched off.
                raise ConfigError(
                    f"the binding for role {resolved_role.value!r} is disabled; "
                    "resolve without it or enable it in the binding store",
                    role=resolved_role.value,
                )
            if binding.model:
                return ModelChoice(
                    role=resolved_role,
                    tier=tier,
                    model=binding.model,
                    source=ModelSource.BINDING_OVERRIDE,
                    max_input_tokens=binding.max_input_tokens,
                    max_output_tokens=binding.max_output_tokens,
                )
        account_provider = self._coerce_provider(provider)
        return ModelChoice(
            role=resolved_role,
            tier=tier,
            model=self.model_for(account_provider, tier),
            source=ModelSource.PROVIDER_DEFAULT,
        )

    def table(self) -> dict[str, str]:
        """Role -> tier for every binding role, for ``/api/config`` and telemetry."""
        return {role.value: self.tier_for(role).value for role in BindingRole}

    def models_for(self, provider: AccountProvider | str) -> dict[str, str]:
        """Tier -> model id for one provider."""
        account_provider = self._coerce_provider(provider)
        table = self._models.get(account_provider)
        if table is None:
            raise ConfigError(
                f"no model table for provider {account_provider.value!r}",
                provider=account_provider.value,
            )
        return {tier.value: model for tier, model in table.items()}

    @staticmethod
    def _coerce_provider(provider: AccountProvider | str | None) -> AccountProvider:
        """Provider enum from its wire string; a missing provider is a config error."""
        if provider is None:
            raise ConfigError(
                "model resolution needs a provider or a binding that pins one; "
                "pass provider=AccountProvider.ANTHROPIC (or the account's provider)"
            )
        if isinstance(provider, AccountProvider):
            return provider
        try:
            return AccountProvider(provider)
        except ValueError as exc:
            known = ", ".join(item.value for item in AccountProvider)
            raise ConfigError(
                f"unknown provider {provider!r}; known providers: {known}", provider=provider
            ) from exc
