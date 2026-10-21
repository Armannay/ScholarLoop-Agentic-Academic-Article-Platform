"""Skill lint CLI: fail a build that ships a malformed or oversized skill.

Usage::

    python -m app.skills.lint --skills-dir skills

Exit codes: ``0`` clean, ``1`` at least one problem. The command is wired into
``make check`` so a skill cannot quietly grow past its declared ``max_tokens`` or the
project-wide 400-token ceiling.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.skills.loader import (
    DEFAULT_SKILLS_DIR,
    STARTER_SKILL_NAMES,
    STARTER_SKILL_TOKEN_CEILING,
    SkillLoader,
    lint_skills,
)

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``python -m app.skills.lint``."""
    parser = argparse.ArgumentParser(
        prog="app.skills.lint",
        description="Validate skills/<name>/SKILL.md front matter and token budgets.",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=DEFAULT_SKILLS_DIR,
        help="skills directory to lint (default: the shipped skills/)",
    )
    parser.add_argument(
        "--ceiling",
        type=int,
        default=STARTER_SKILL_TOKEN_CEILING,
        help="project-wide per-skill token ceiling",
    )
    parser.add_argument(
        "--require",
        nargs="*",
        default=list(STARTER_SKILL_NAMES),
        help="skill names that must be installed (default: the seven starter skills)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the lint; returns the process exit code."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    problems = lint_skills(
        args.skills_dir,
        ceiling=args.ceiling,
        required=tuple(args.require),
    )
    write = sys.stdout.write
    if problems:
        for problem in problems:
            write(f"FAIL {problem.render()}\n")
        write(f"{len(problems)} problem(s) in {args.skills_dir}\n")
        return 1
    inspection = SkillLoader(args.skills_dir).inspect()
    for audit in inspection.audits:
        write(
            f"ok   {audit.name:<24} {audit.tokens:>4}/{audit.declared_max_tokens} tokens"
            f" (ceiling {args.ceiling})\n"
        )
    write(f"{len(inspection.audits)} skill(s) OK in {args.skills_dir}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
