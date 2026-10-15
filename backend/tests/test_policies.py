"""Policy loader: defaults, deep merge, project overrides and rejection behaviour.

The loader is the guard between a YAML file and the deterministic loop, so these
tests deliberately try to break it: unknown keys, wrong types, impossible budgets and
silently malformed documents must all fail loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.enums import BindingRole, ModelTier
from app.core.errors import ConfigError, PolicyError
from app.core.policies import DEFAULT_POLICIES, Policies, deep_merge, load_policies

pytestmark = pytest.mark.config


def write_yaml(tmp_path: Path, name: str, text: str) -> Path:
    """Write a YAML file into the test's temp directory."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------- shipped file
def test_shipped_file_matches_code_defaults(policies: Policies) -> None:
    """config/policies.yaml documents the code defaults exactly; drift is a bug."""
    assert policies.model_dump() == DEFAULT_POLICIES.model_dump()


def test_shipped_defaults_are_the_agreed_numbers(policies: Policies) -> None:
    """The numbers other steps rely on, pinned once."""
    assert policies.run_token_cap == 800_000
    assert policies.iteration_token_cap == 120_000
    assert policies.call_input_cap == 24_000
    assert policies.max_iterations == 12
    assert policies.max_parallel == 3
    assert policies.digest_max_tokens == 2_000
    assert policies.recent_window == 3
    assert policies.compact_trigger == 0.8
    assert policies.q_min == 70
    assert (policies.plateau.window, policies.plateau.min_gain) == (3, 1.0)
    assert (policies.regression.delta, policies.regression.window) == (3.0, 2)
    assert policies.oscillation.window == 4
    assert (policies.diminishing.min_gain_per_10k, policies.diminishing.window) == (0.2, 2)
    assert policies.issue_churn == 0.3
    assert policies.error_streak == 3


def test_model_tiers_follow_the_agreed_mapping(policies: Policies) -> None:
    """Every role has a tier, including the two non-authoring ones."""
    tiers = {role: policies.tier_for(role) for role in BindingRole}
    assert tiers[BindingRole.PLANNER] is ModelTier.STRONG
    assert tiers[BindingRole.ARCHITECT] is ModelTier.MID
    assert tiers[BindingRole.RESEARCHER] is ModelTier.MID
    assert tiers[BindingRole.WRITER] is ModelTier.MID
    assert tiers[BindingRole.REVIEWER] is ModelTier.STRONG
    assert tiers[BindingRole.VALIDATOR] is ModelTier.CHEAP
    assert tiers[BindingRole.COMPACTOR] is ModelTier.CHEAP


def test_derived_helpers(policies: Policies) -> None:
    """The digest ceilings derived from the policy values."""
    assert policies.digest_budget_bytes == 8_000
    assert policies.compact_at_tokens == 1_600


# -------------------------------------------------------------------- deep merge
def test_deep_merge_replaces_scalars_and_recurses_into_mappings() -> None:
    """A nested override touches one key and leaves its siblings alone."""
    base = {"run_token_cap": 800_000, "plateau": {"window": 3, "min_gain": 1.0}}
    merged = deep_merge(base, {"plateau": {"min_gain": 2.5}}, where="policies")
    assert merged == {"run_token_cap": 800_000, "plateau": {"window": 3, "min_gain": 2.5}}
    assert base["plateau"] == {"window": 3, "min_gain": 1.0}, "base must not be mutated"


def test_deep_merge_rejects_unknown_keys_with_the_dotted_path() -> None:
    """A typo must name itself, not just the file."""
    with pytest.raises(ConfigError, match=r"policies\.plateau\.min_gains"):
        deep_merge(
            {"plateau": {"min_gain": 1.0}}, {"plateau": {"min_gains": 2.0}}, where="policies"
        )


def test_unknown_top_level_policy_key_is_rejected(tmp_path: Path) -> None:
    """A misspelled policy silently keeping its default is the worst outcome."""
    path = write_yaml(
        tmp_path, "policies.yaml", "schema_version: 1\npolicies:\n  run_token_cap2: 5\n"
    )
    with pytest.raises(ConfigError, match="unknown policy key"):
        load_policies(path)


def test_unknown_top_level_document_key_is_rejected(tmp_path: Path) -> None:
    """The document has exactly three allowed top-level keys."""
    path = write_yaml(tmp_path, "policies.yaml", "schema_version: 1\npolicies: {}\npoliciez: {}\n")
    with pytest.raises(ConfigError, match="unexpected top-level keys"):
        load_policies(path)


def test_wrong_schema_version_is_rejected(tmp_path: Path) -> None:
    """The loader refuses a document from a future format."""
    path = write_yaml(tmp_path, "policies.yaml", "schema_version: 2\npolicies: {}\n")
    with pytest.raises(ConfigError, match="schema_version must be 1"):
        load_policies(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    """A missing config file is an error, not an empty policy set."""
    with pytest.raises(ConfigError, match="not found"):
        load_policies(tmp_path / "nope.yaml")


def test_non_mapping_policies_block_is_rejected(tmp_path: Path) -> None:
    """``policies: 5`` is a broken file, not a policy."""
    path = write_yaml(tmp_path, "policies.yaml", "schema_version: 1\npolicies: 5\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_policies(path)


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    """A YAML syntax error must not be swallowed."""
    path = write_yaml(tmp_path, "policies.yaml", "schema_version: 1\npolicies: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_policies(path)


# --------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_iterations", 0),
        ("max_iterations", 500),
        ("compact_trigger", 1.5),
        ("compact_trigger", 0),
        ("q_min", 101),
        ("max_parallel", 0),
        ("error_streak", 0),
        ("issue_churn", 1.5),
        ("digest_max_tokens", 50),
    ],
)
def test_out_of_range_values_are_rejected(tmp_path: Path, key: str, value: float) -> None:
    """Values must fail validation, with the offending key in the message."""
    path = write_yaml(
        tmp_path, "policies.yaml", f"schema_version: 1\npolicies:\n  {key}: {value}\n"
    )
    with pytest.raises(PolicyError) as excinfo:
        load_policies(path)
    assert key in str(excinfo.value) or key in str(excinfo.value.context)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iteration_token_cap": 900_000},  # iteration cap above the run cap
        {"call_input_cap": 130_000},  # one call could eat the whole iteration
        {"call_input_cap": 500, "digest_max_tokens": 1_000},  # digest above one call
    ],
)
def test_budget_nesting_is_enforced(tmp_path: Path, overrides: dict[str, int]) -> None:
    """Budgets nest: call <= iteration <= run, and the digest stays below one call."""
    body = "".join(f"  {key}: {value}\n" for key, value in overrides.items())
    path = write_yaml(tmp_path, "policies.yaml", f"schema_version: 1\npolicies:\n{body}")
    with pytest.raises(PolicyError, match="must"):
        load_policies(path)


# ------------------------------------------------------------------- project merge
PROJECT_DOC = """\
schema_version: 1
policies:
  call_input_cap: 20000
projects:
  thesis:
    q_min: 85
    plateau:
      min_gain: 2.5
"""


def test_project_override_deep_merges(tmp_path: Path) -> None:
    """A project block changes only what it names."""
    path = write_yaml(tmp_path, "policies.yaml", PROJECT_DOC)
    base = load_policies(path)
    thesis = load_policies(path, project="thesis")

    assert base.q_min == 70 and base.plateau.min_gain == 1.0
    assert thesis.q_min == 85
    assert thesis.plateau.min_gain == 2.5
    assert thesis.plateau.window == 3, "unmentioned nested keys keep their value"
    assert thesis.run_token_cap == DEFAULT_POLICIES.run_token_cap


def test_unknown_project_is_rejected(tmp_path: Path) -> None:
    """A typo in the project name must not silently return the base policies."""
    path = write_yaml(tmp_path, "policies.yaml", PROJECT_DOC)
    with pytest.raises(ConfigError, match="no override block for project"):
        load_policies(path, project="thesys")


def test_unknown_key_inside_a_project_block_is_rejected(tmp_path: Path) -> None:
    """Project blocks get the same key checking as the base block."""
    path = write_yaml(
        tmp_path, "policies.yaml", "schema_version: 1\nprojects:\n  thesis:\n    q_minn: 85\n"
    )
    with pytest.raises(ConfigError, match=r"projects\.thesis\.q_minn"):
        load_policies(path, project="thesis")


def test_external_overrides_file_wins_over_the_base_file(tmp_path: Path) -> None:
    """Precedence: base file < base project < overrides file < overrides project."""
    base = write_yaml(tmp_path, "policies.yaml", PROJECT_DOC)
    overrides = write_yaml(
        tmp_path,
        "overrides.yaml",
        "schema_version: 1\npolicies:\n  q_min: 75\nprojects:\n  thesis:\n    q_min: 90\n",
    )

    assert load_policies(base, overrides_path=overrides).q_min == 75
    assert load_policies(base, overrides_path=overrides, project="thesis").q_min == 90


def test_overrides_file_is_optional(tmp_path: Path) -> None:
    """A configured-but-absent overrides path is not an error."""
    base = write_yaml(tmp_path, "policies.yaml", "schema_version: 1\npolicies:\n  q_min: 72\n")
    policies = load_policies(base, overrides_path=tmp_path / "absent.yaml")
    assert policies.q_min == 72


def test_example_project_file_loads(policies: Policies, config_dir: Path) -> None:
    """The shipped example must stay valid, since it is the documented format."""
    example = config_dir / "projects.example.yaml"
    loaded = load_policies(
        config_dir / "policies.yaml", overrides_path=example, project="thesis-chapter"
    )
    assert loaded.q_min == 80
    assert loaded.max_iterations == 16
    assert loaded.plateau.window == 4
    assert loaded.plateau.min_gain == 2.0
    assert loaded.model_tiers.writer is ModelTier.STRONG
    assert loaded.call_input_cap == 20_000, "the file-level block applies first"
    assert loaded.run_token_cap == policies.run_token_cap

    short = load_policies(
        config_dir / "policies.yaml", overrides_path=example, project="short-essay"
    )
    assert (short.run_token_cap, short.iteration_token_cap) == (200_000, 40_000)


def test_base_policies_can_be_injected(tmp_path: Path) -> None:
    """Tests and tools can layer a file on top of an explicit base."""
    path = write_yaml(tmp_path, "policies.yaml", "schema_version: 1\npolicies:\n  q_min: 55\n")
    loaded = load_policies(path, base=DEFAULT_POLICIES)
    assert loaded.q_min == 55
