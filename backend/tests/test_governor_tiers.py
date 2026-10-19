"""Model tiers: policies pick the tier, an AgentBinding overrides the model.

Tier-to-model is a deployment decision (and vendor ids change), so the resolver
must be explicit: no silent fallback to the expensive model, no guessing when a
tier has no mapping.
"""

from __future__ import annotations

import pytest

from app.contracts.binding import AgentBinding
from app.contracts.enums import BindingRole, ModelTier
from app.core.policies import Policies
from app.governor.tiers import (
    DEFAULT_TIER_MODELS,
    describe_resolution,
    resolve_model,
    resolve_tier,
    tier_for_role,
)
from tests.factories import sample_ulids

pytestmark = pytest.mark.config


def binding(role: BindingRole, model: str, *, enabled: bool = True) -> AgentBinding:
    """A minimal binding with two accounts (failover needs two)."""
    first, second = sample_ulids(2)
    return AgentBinding(
        role=role,
        account_ids=[first, second],
        strategy="failover",
        model=model,
        max_input_tokens=24_000,
        max_output_tokens=4_000,
        enabled=enabled,
    )


# ------------------------------------------------------------------ policy tiers
@pytest.mark.parametrize(
    ("role", "tier"),
    [
        ("planner", ModelTier.STRONG),
        ("architect", ModelTier.MID),
        ("researcher", ModelTier.MID),
        ("writer", ModelTier.MID),
        ("reviewer", ModelTier.STRONG),
        ("validator", ModelTier.CHEAP),
        ("compactor", ModelTier.CHEAP),
    ],
)
def test_tiers_come_from_policies(policies: Policies, role: str, tier: ModelTier) -> None:
    """The shipped mapping is the contract every other step relies on."""
    assert tier_for_role(role, policies) is tier
    assert resolve_tier(BindingRole(role), policies) is tier


def test_unknown_role_gets_the_mid_tier_not_the_strong_one(policies: Policies) -> None:
    """A new role must not silently inherit the most expensive model."""
    assert tier_for_role("auditor", policies) is ModelTier.MID


# ---------------------------------------------------------------- tier -> model
def test_each_tier_resolves_to_its_default_model(policies: Policies) -> None:
    """Without a binding, the tier table names the model."""
    assert resolve_model("planner", policies) == DEFAULT_TIER_MODELS[ModelTier.STRONG]
    assert resolve_model("writer", policies) == DEFAULT_TIER_MODELS[ModelTier.MID]
    assert resolve_model("validator", policies) == DEFAULT_TIER_MODELS[ModelTier.CHEAP]


def test_custom_tier_table_accepts_string_keys(policies: Policies) -> None:
    """YAML gives string keys; the resolver coerces them."""
    table = {"strong": "model-strong", "mid": "model-mid", "cheap": "model-cheap"}

    assert resolve_model("planner", policies, tier_models=table) == "model-strong"
    assert resolve_model("validator", policies, tier_models=table) == "model-cheap"


def test_missing_tier_mapping_is_an_error_not_a_guess(policies: Policies) -> None:
    """An incomplete table must fail loudly; a silent default costs money."""
    with pytest.raises(ValueError, match="no model configured for tier"):
        resolve_model("planner", policies, tier_models={ModelTier.CHEAP: "only-cheap"})


def test_unknown_tier_key_is_rejected(policies: Policies) -> None:
    """A typo in the table is a configuration bug."""
    with pytest.raises(ValueError, match="unknown tier"):
        resolve_model("writer", policies, tier_models={"medium": "x"})


# ------------------------------------------------------------------- overrides
def test_enabled_binding_overrides_the_tier(policies: Policies) -> None:
    """A user's explicit model choice wins, even against a cheap tier."""
    chosen = binding(BindingRole.VALIDATOR, "my-fine-tuned-validator")

    assert resolve_model("validator", policies, binding=chosen) == "my-fine-tuned-validator"


def test_disabled_binding_is_ignored(policies: Policies) -> None:
    """Disabled means disabled: fall back to the policy tier."""
    disabled = binding(BindingRole.WRITER, "unused-model", enabled=False)

    assert resolve_model("writer", policies, binding=disabled) == DEFAULT_TIER_MODELS[ModelTier.MID]


def test_whitespace_only_binding_model_falls_back(policies: Policies) -> None:
    """A blank model cannot become the model id (constructed via model_copy, since
    the contract itself refuses blanks at the boundary)."""
    blank = binding(BindingRole.WRITER, "placeholder").model_copy(update={"model": "   "})

    assert resolve_model("writer", policies, binding=blank) == DEFAULT_TIER_MODELS[ModelTier.MID]


def test_describe_resolution_explains_the_source(policies: Policies) -> None:
    """Logs must say whether the binding or the tier decided."""
    chosen = binding(BindingRole.WRITER, "custom-writer")

    from_binding = describe_resolution("writer", policies, binding=chosen)
    from_tier = describe_resolution("writer", policies)

    assert from_binding == {
        "role": "writer",
        "tier": "mid",
        "model": "custom-writer",
        "source": "binding",
        "binding_id": None,
    }
    assert from_tier["source"] == "tier"
    assert from_tier["model"] == DEFAULT_TIER_MODELS[ModelTier.MID]
