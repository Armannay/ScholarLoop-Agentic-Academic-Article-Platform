"""The CI secret guard, and the guarantee that the repository is clean right now.

The scan is run against the real repository in a test, so a key pasted into a fixture,
a doc, a workflow file or a config by any later step fails here - not in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scripts.secret_scan import iter_candidate_files, main, scan, scan_file

pytestmark = pytest.mark.contracts


def test_repository_is_free_of_secret_shaped_strings(repo_root: Path) -> None:
    """The whole tree, as CI sees it."""
    findings = scan(repo_root)
    assert findings == [], "\n".join(
        f"{path}:{line}: [{rule}] {text}" for path, line, rule, text in findings
    )


def test_scan_detects_a_key_in_a_file(tmp_path: Path) -> None:
    """The guard actually fires - constructed at runtime so this file stays clean."""
    planted = tmp_path / "leaked.txt"
    planted.write_text("key = " + "sk-ant-" + "q" * 28 + "\n", encoding="utf-8")

    findings = scan_file(planted)
    assert findings, "the scanner missed a planted key"
    assert findings[0][1] == "anthropic key"
    assert findings[0][0] == 1


def test_scan_ignores_placeholders_and_documentation(tmp_path: Path) -> None:
    """Documented placeholders must not break the build."""
    (tmp_path / "doc.md").write_text(
        'Set api_key = "your-key-here" or api_key = "placeholder-value".\n',
        encoding="utf-8",
    )
    assert scan(tmp_path) == []


def test_scan_skips_binary_and_vendor_directories(tmp_path: Path) -> None:
    """node_modules and friends are not the project's responsibility."""
    vendor = tmp_path / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text(
        "const k = '" + "sk-ant-" + "w" * 30 + "';\n", encoding="utf-8"
    )
    (tmp_path / "image.png").write_bytes(b"\x89PNG" + b"sk-ant-" + b"z" * 30)
    assert scan(tmp_path) == []


def test_candidate_files_exclude_caches_and_the_scanner_itself(repo_root: Path) -> None:
    """The scanner cannot flag itself, and caches are out of scope."""
    candidates = {path.name for path in iter_candidate_files(repo_root)}
    assert "secret_scan.py" not in candidates
    assert "__pycache__" not in " ".join(str(path) for path in iter_candidate_files(repo_root))


def test_cli_exit_codes(tmp_path: Path, repo_root: Path) -> None:
    """CI depends on the exit code, not the output."""
    assert main(["--root", str(repo_root)]) == 0

    (tmp_path / "bad.txt").write_text("token sk-ant-" + "v" * 30 + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path)]) == 1


def test_no_env_file_is_committed(repo_root: Path) -> None:
    """The repo ships .env.example only: a real .env must never be tracked."""
    assert (repo_root / ".env.example").exists()
    assert not (repo_root / ".env").exists()
    tracked = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in tracked
