"""CI guard for HARD RULE 2: no secret-shaped strings anywhere in the tree.

The detection rules live in :mod:`app.core.redaction`, which is also what the logging
filter and the API error path use - so the thing that fails CI and the thing that
redacts logs can never drift apart.

Runnable both as a module and as a plain script path (CI and humans disagree about
which is nicer), hence the explicit ``sys.path`` bootstrap below:

    python -m app.scripts.secret_scan --root .
    python backend/app/scripts/secret_scan.py --root .
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python backend/app/scripts/secret_scan.py` from a fresh checkout: this file
# is backend/app/scripts/x.py, so the import root is parents[2] == backend/.
if __package__ in (None, ""):
    _BACKEND_ROOT = Path(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.redaction import find_secrets

__all__ = ["iter_candidate_files", "main", "scan", "scan_file"]

#: file name -> skip entirely
SKIP_FILES = {
    ".env",
    "secret_scan.py",  # this file necessarily names the shapes it hunts
    "policies.yaml",  # commented examples only; the scanner reads lines, not YAML
}
#: directory name -> skip entirely
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}
#: extension -> skip (binary-ish)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".sqlite3"}


def iter_candidate_files(root: Path) -> list[Path]:
    """Every text-ish file under ``root`` that is not excluded."""
    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
            continue
        candidates.append(path)
    return candidates


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line_number, rule, line)`` for every finding in ``path``."""
    findings: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, _fragment in find_secrets(line):
            findings.append((number, rule, line.strip()[:160]))
    return findings


def scan(root: Path) -> list[tuple[Path, int, str, str]]:
    """Scan a tree and return all findings as ``(path, line, rule, text)``."""
    results: list[tuple[Path, int, str, str]] = []
    for path in iter_candidate_files(root):
        for number, rule, line in scan_file(path):
            results.append((path, number, rule, line))
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 1 when any secret-shaped string is found."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    args = parser.parse_args(argv)

    findings = scan(args.root)
    for path, number, rule, line in findings:
        print(f"{path}:{number}: [{rule}] {line}", file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} potential secret(s) found. See HARD RULE 2.", file=sys.stderr)
        return 1
    print(f"secret scan clean under {args.root}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    raise SystemExit(main())
