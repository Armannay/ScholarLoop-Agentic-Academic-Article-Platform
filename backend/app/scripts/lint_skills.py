"""Check every skill body against its declared ``max_tokens`` value.

Run from the repository root with ``python -m app.scripts.lint_skills --root skills``
(or ``make skills-lint`` after installing the development environment).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.skills.loader import SkillFormatError, SkillLoader


def lint_skills(root: Path) -> list[str]:
    """Return all format and token-budget errors found in ``root``."""
    try:
        loader = SkillLoader(root)
        skill_names = [item["name"] for item in loader.index()]
    except SkillFormatError as exc:
        return [str(exc)]

    errors: list[str] = []
    for name in skill_names:
        try:
            loader.load(name)
        except SkillFormatError as exc:
            errors.append(str(exc))
    return errors


def main() -> int:
    """CLI entry point; exit non-zero if any skill fails validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("skills"))
    args = parser.parse_args()
    errors = lint_skills(args.root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"skills lint passed: {len(list(args.root.glob('*/SKILL.md')))} skill(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
