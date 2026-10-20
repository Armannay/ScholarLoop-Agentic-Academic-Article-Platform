"""Skill lint: the command fails on oversized or malformed skills, and passes the seven."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills import lint
from app.skills.loader import (
    STARTER_SKILL_NAMES,
    SkillLoader,
    lint_skills,
)
from tests.test_skills_loader import make_task, write_skill

pytestmark = pytest.mark.skills


def test_lint_passes_the_seven_shipped_skills(repo_root: Path) -> None:
    """The deliverable gate: all seven starter skills are valid and under 400 tokens."""
    problems = lint_skills(repo_root / "skills")
    assert problems == ()


def test_lint_cli_exits_zero_and_reports_every_skill(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = lint.main(["--skills-dir", str(repo_root / "skills")])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "7 skill(s) OK" in out
    for name in STARTER_SKILL_NAMES:
        assert name in out


def test_lint_fails_when_a_body_exceeds_its_declared_max_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The declared limit is the contract; the lint makes breaking it visible."""
    write_skill(tmp_path, "alpha", body="word " * 400, max_tokens=10, triggers=("draft",))

    problems = lint_skills(tmp_path, ceiling=1_000, required=())
    assert [problem.code for problem in problems] == ["body_too_large"]

    exit_code = lint.main(["--skills-dir", str(tmp_path), "--ceiling", "1000", "--require"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "body_too_large" in out
    assert "FAIL alpha" in out


def test_lint_fails_when_the_project_ceiling_is_exceeded(tmp_path: Path) -> None:
    """A skill may declare 400 tokens and still exceed a stricter project ceiling."""
    write_skill(tmp_path, "alpha", body="word " * 60, max_tokens=400)
    problems = lint_skills(tmp_path, ceiling=10, required=())
    assert [problem.code for problem in problems] == ["ceiling_exceeded"]


def test_lint_reports_missing_starter_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", triggers=("draft",))
    problems = lint_skills(tmp_path, required=STARTER_SKILL_NAMES)
    missing = {problem.skill for problem in problems if problem.code == "missing"}
    assert missing == set(STARTER_SKILL_NAMES)


def test_lint_reports_malformed_metadata_without_crashing(tmp_path: Path) -> None:
    """One broken skill must not stop the audit of the others."""
    write_skill(tmp_path, "good", triggers=("draft",))
    write_skill(tmp_path, "bad", omit=("roles",))
    problems = lint_skills(tmp_path, required=())
    assert any(problem.skill == "bad" for problem in problems)
    assert not any(problem.skill == "good" for problem in problems)


def test_lint_reports_a_missing_directory(tmp_path: Path) -> None:
    problems = lint_skills(tmp_path / "does-not-exist", required=())
    assert [problem.code for problem in problems] == ["missing_dir"]


def test_lint_parser_defaults_to_the_shipped_directory() -> None:
    parser = lint.build_parser()
    args = parser.parse_args([])
    assert args.ceiling == 400
    assert tuple(args.require) == STARTER_SKILL_NAMES
    assert args.skills_dir.name == "skills"


def test_loader_and_lint_agree_on_every_shipped_skill(repo_root: Path) -> None:
    """Lint measures the same bodies the loader will put in a prompt."""
    inspection = SkillLoader(repo_root / "skills").inspect()
    assert {audit.name for audit in inspection.audits} == set(STARTER_SKILL_NAMES)


def test_selection_uses_only_lint_clean_bodies(repo_root: Path) -> None:
    """The shipped skills can actually be selected for a real task."""
    loader = SkillLoader(repo_root / "skills")
    selection = loader.select(make_task())
    assert 1 <= len(selection.skills) <= 2
    assert selection.tokens <= selection.budget
