"""Prompt assets and architecture docs are deliverables too, so they are tested.

The rules files are *prompt* material with a hard size budget (HARD RULE 3): a rules
file that quietly grows to 2,000 tokens taxes every single call of its role. These
tests keep them small and keep the HARD RULES in them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.contracts.enums import AgentRole
from app.providers.base import estimate_tokens

pytestmark = pytest.mark.config

#: Budget per rules file, as agreed for the shared context.
RULES_TOKEN_BUDGET = 400

REQUIRED_HARD_RULE_PHRASES = (
    "never fabricate",
    "api key",
    "patch",
    "tool output",
    "mockprovider",
    "contract",
)


def rules_files(repo_root: Path) -> list[Path]:
    """Every rules file, root first then per-role."""
    root_rules = repo_root / "rules" / "root.rules.md"
    agent_rules = sorted((repo_root / "rules" / "agents").glob("*.rules.md"))
    return [root_rules, *agent_rules]


def test_rules_tree_is_complete(repo_root: Path) -> None:
    """One shared file plus one file per agent role - no gaps, no strays."""
    agent_files = sorted((repo_root / "rules" / "agents").glob("*.rules.md"))
    assert [path.stem.split(".")[0] for path in agent_files] == sorted(
        role.value for role in AgentRole
    )
    assert (repo_root / "rules" / "root.rules.md").exists()


@pytest.mark.parametrize(
    "path", ["rules/root.rules.md"] + [f"rules/agents/{role.value}.rules.md" for role in AgentRole]
)
def test_rules_file_is_within_the_token_budget(repo_root: Path, path: str) -> None:
    """≤ 400 tokens each: prompt cost is a design constraint, not an afterthought."""
    text = (repo_root / path).read_text(encoding="utf-8")
    tokens = estimate_tokens(text)
    assert tokens <= RULES_TOKEN_BUDGET, f"{path} is {tokens} tokens (budget {RULES_TOKEN_BUDGET})"


@pytest.mark.parametrize("path", [f"rules/agents/{role.value}.rules.md" for role in AgentRole])
def test_agent_rules_point_at_the_root_rules(repo_root: Path, path: str) -> None:
    """Every agent file defers to the shared hard rules instead of restating them."""
    text = (repo_root / path).read_text(encoding="utf-8")
    assert "rules/root.rules.md" in text


def test_root_rules_contain_the_hard_rules(repo_root: Path) -> None:
    """The six hard rules must be present, in imperative form."""
    text = (repo_root / "rules" / "root.rules.md").read_text(encoding="utf-8").lower()
    for phrase in REQUIRED_HARD_RULE_PHRASES:
        assert phrase in text, f"root.rules.md does not mention {phrase!r}"
    assert "needs_user_input" in text


@pytest.mark.parametrize(
    "path", ["rules/root.rules.md"] + [f"rules/agents/{role.value}.rules.md" for role in AgentRole]
)
def test_rules_are_imperative_and_ordered(repo_root: Path, path: str) -> None:
    """Rules are numbered instructions; a prose essay is not a rules file."""
    lines = (repo_root / path).read_text(encoding="utf-8").splitlines()
    numbered = [line for line in lines if re.match(r"^\d+\.\s+\S", line)]
    assert len(numbered) >= 5, f"{path} has only {len(numbered)} numbered rules"
    assert numbered[0].startswith("1.")


def test_rules_files_contain_no_secrets(repo_root: Path) -> None:
    """Rules are prompt material, which is another way of saying 'readable by a model'."""
    from app.core.redaction import find_secrets

    for path in rules_files(repo_root):
        assert find_secrets(path.read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------- docs
def test_architecture_doc_covers_the_state_machine(repo_root: Path) -> None:
    """The documented states are the ones the orchestrator will implement."""
    text = (repo_root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for state in (
        "INTAKE",
        "PLANNING",
        "EXECUTING",
        "VALIDATING",
        "DECIDING",
        "CHECKPOINT",
        "REPLANNING",
        "DONE",
        "STOPPED",
    ):
        assert state in text, f"ARCHITECTURE.md does not document {state}"
    assert "one iteration" in text.lower()


def test_architecture_doc_names_every_component(repo_root: Path) -> None:
    """Each module in the REPO layout has a stated responsibility."""
    text = (repo_root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for module in (
        "contracts",
        "core",
        "accounts",
        "providers",
        "governor",
        "memory",
        "article",
        "agents",
        "planner",
        "orchestrator",
        "api",
    ):
        assert f"app/{module}/" in text, f"ARCHITECTURE.md does not describe app/{module}/"


def test_architecture_doc_stays_within_two_pages(repo_root: Path) -> None:
    """'<= 2 pages' means roughly 1,400 words; drift here is a documentation smell."""
    text = (repo_root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    words = len(text.split())
    assert words <= 1_500, f"ARCHITECTURE.md is {words} words"


def test_decisions_doc_has_a_template_and_five_adrs(repo_root: Path) -> None:
    """ADRs are how the next steps learn why the foundation looks like this."""
    text = (repo_root / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    assert "## Template" in text
    for field in ("Status:", "Date:", "Context:", "Decision:", "Alternatives:", "Consequences:"):
        assert field in text, f"ADR template is missing {field}"

    numbers = re.findall(r"^## ADR-(\d{4}) ", text, flags=re.MULTILINE)
    assert numbers[:5] == ["0001", "0002", "0003", "0004", "0005"]
    assert len(numbers) >= 5


def test_readme_points_at_the_entry_points(repo_root: Path) -> None:
    """A new teammate should reach the docs from the README."""
    text = (repo_root / "README.md").read_text(encoding="utf-8")
    for pointer in (
        "docs/ARCHITECTURE.md",
        "docs/DECISIONS.md",
        "docs/schemas/",
        "make check",
        ".env.example",
    ):
        assert pointer in text, f"README does not mention {pointer}"
