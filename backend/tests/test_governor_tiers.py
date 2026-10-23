"""Model tiers: policies pick the class, the binding picks the id.

Every assertion runs against the shipped ``config/policies.yaml`` (the ``policies``
fixture), so a policy edit that moves a role to another tier fails here instead of
quietly changing the price of a run.
"""

from __future__ import annotations

import pytest

from app.contracts.binding import AgentBinding
from app.contracts.enums import AccountProvider, BindingRole, BindingStrategy, ModelTier
from app.core.errors import ConfigError
from app.core.policies import Policies
from app.governor import (
    DEFAULT_MODELS,
    ModelChoice,
    ModelSource,
    TierResolver,
    as_binding_role,
)
from tests.factories import sample_ulid

pytestmark = pytest.mark.governor


def make_binding(
    *,
    role: BindingRole = BindingRole.WRITER,
    model: str = "claude-sonnet-4-5",
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 8_000,
) -> AgentBinding:
    return AgentBinding(
        role=role,
        account_ids=[sample_ulid(7)],
        strategy=BindingStrategy.DEDICATED,
        model=model,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )


# --------------------------------------------------------------------------- tiers
def test_tiers_come_from_the_shipped_policies(policies: Policies):
    resolver = TierResolver(policies)

    assert resolver.tier_for("planner") is ModelTier.STRONG
    assert resolver.tier_for(BindingRole.REVIEWER) is ModelTier.STRONG
    assert resolver.tier_for("writer") is ModelTier.MID
    assert resolver.tier_for("architect") is ModelTier.MID
    assert resolver.tier_for("validator") is ModelTier.CHEAP
    assert resolver.tier_for("compactor") is ModelTier.CHEAP


def test_policy_override_moves_a_role_to_another_tier():
    policies = Policies.model_validate(
        {**Policies().model_dump(mode="json"), "model_tiers": {"writer": "strong"}}
    )
    assert TierResolver(policies).tier_for("writer") is ModelTier.STRONG


def test_table_lists_every_binding_role(policies: Policies):
    table = TierResolver(policies).table()

    assert set(table) == {role.value for role in BindingRole}
    assert table["planner"] == "strong"


# --------------------------------------------------------------------------- resolution
def test_resolution_falls_back_to_the_provider_table(policies: Policies):
    resolver = TierResolver(policies)

    choice = resolver.resolve("planner", provider=AccountProvider.ANTHROPIC)

    assert isinstance(choice, ModelChoice)
    assert choice.tier is ModelTier.STRONG
    assert choice.model == DEFAULT_MODELS[AccountProvider.ANTHROPIC][ModelTier.STRONG]
    assert choice.source is ModelSource.PROVIDER_DEFAULT
    assert choice.max_input_tokens is None


def test_binding_override_wins_and_carries_its_caps(policies: Policies):
    resolver = TierResolver(policies)
    binding = make_binding()

    choice = resolver.resolve("writer", binding=binding, provider=AccountProvider.ANTHROPIC)

    assert choice.model == "claude-sonnet-4-5"
    assert choice.source is ModelSource.BINDING_OVERRIDE
    assert choice.is_binding_override
    assert choice.max_input_tokens == 100_000
    assert choice.max_output_tokens == 8_000
    # The tier is still reported: telemetry compares spend per tier, not per model.
    assert choice.tier is ModelTier.MID


def test_openai_compatible_provider_uses_its_own_table(policies: Policies):
    choice = TierResolver(policies).resolve("validator", provider=AccountProvider.OPENAI_COMPATIBLE)

    assert choice.tier is ModelTier.CHEAP
    assert choice.model == DEFAULT_MODELS[AccountProvider.OPENAI_COMPATIBLE][ModelTier.CHEAP]


def test_mock_provider_resolves_to_mock_models(policies: Policies):
    choice = TierResolver(policies).resolve("writer", provider=AccountProvider.MOCK)
    assert choice.model.startswith("mock-")


def test_custom_model_table_overrides_the_built_in_one(policies: Policies):
    resolver = TierResolver(
        policies,
        models={AccountProvider.OPENAI_COMPATIBLE: {ModelTier.CHEAP: "local-cheap-1"}},
    )

    choice = resolver.resolve("validator", provider="openai_compatible")

    assert choice.model == "local-cheap-1"
    assert resolver.models_for("openai_compatible") == {"cheap": "local-cheap-1"}


# --------------------------------------------------------------------------- errors
def test_a_disabled_binding_does_not_resolve_a_model(policies: Policies):
    resolver = TierResolver(policies)
    disabled = make_binding(role=BindingRole.WRITER)
    disabled = disabled.model_copy(update={"enabled": False})

    with pytest.raises(ConfigError, match="disabled"):
        resolver.resolve("writer", binding=disabled, provider=AccountProvider.ANTHROPIC)


def test_a_binding_for_another_role_is_rejected(policies: Policies):
    resolver = TierResolver(policies)
    binding = make_binding(role=BindingRole.REVIEWER)

    with pytest.raises(ValueError, match="reviewer"):
        resolver.resolve("writer", binding=binding)


def test_no_provider_and_no_binding_is_a_config_error(policies: Policies):
    with pytest.raises(ConfigError, match="provider"):
        TierResolver(policies).resolve("writer")


def test_unknown_role_is_a_config_error(policies: Policies):
    with pytest.raises(ConfigError, match="known roles"):
        TierResolver(policies).resolve("poet", provider=AccountProvider.ANTHROPIC)


def test_unknown_provider_is_a_config_error(policies: Policies):
    with pytest.raises(ConfigError, match="known providers"):
        TierResolver(policies).resolve("writer", provider="not-a-provider")


def test_missing_tier_in_a_custom_table_is_a_config_error(policies: Policies):
    resolver = TierResolver(policies, models={AccountProvider.ANTHROPIC: {ModelTier.CHEAP: "tiny"}})

    with pytest.raises(ConfigError, match="strong"):
        resolver.resolve("planner", provider=AccountProvider.ANTHROPIC)


def test_role_coercion_accepts_strings_and_enums():
    assert as_binding_role("planner") is BindingRole.PLANNER
    assert as_binding_role(BindingRole.WRITER) is BindingRole.WRITER
    with pytest.raises(ConfigError):
        as_binding_role("nope")
