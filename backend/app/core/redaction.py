"""Secret-shape detection and redaction (HARD RULE 2).

One definition, three uses:

* the CI scanner (``app/scripts/secret_scan.py``) fails a build that contains one;
* the logging filter rewrites them before a line is emitted;
* API error bodies pass through :func:`redact` before leaving the process.

This is pattern matching, not a secret manager: it exists to make the *accidental*
leak (a key pasted into a config value, a prompt, a log line or a fixture) loud and
harmless. Real key material never enters the process except through the account store.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["PATTERNS", "PLACEHOLDER_MARKERS", "REDACTED", "find_secrets", "redact"]

REDACTED: Final[str] = "[REDACTED]"

#: Spelled-out placeholders that are *allowed* to appear in examples and fixtures.
PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "placeholder",
    "example",
    "redacted",
    "dummy",
    "fake",
    "not-a-real",
    "xxxx",
    "your-",
    "<your",
    "${",
    "changeme",
    "test-key",
)

#: Ordered rules: name -> matcher. Names appear in scanner output and in log lines.
PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    (
        "hardcoded key assignment",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\s*[:=]\s*"""
            r"""["'][^"'\s]{16,}["']"""
        ),
    ),
)


def _is_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return ``(rule_name, matched_text)`` for every secret-shaped string in ``text``.

    A match that also contains a documented placeholder marker is not a finding, so
    ``api_key = "your-key-here"`` in a fixture stays legitimate.
    """
    findings: list[tuple[str, str]] = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            fragment = match.group(0)
            if _is_placeholder(fragment):
                continue
            findings.append((name, fragment))
    return findings


def redact(text: str) -> str:
    """Replace every secret-shaped string in ``text`` with ``[REDACTED]``."""
    redacted = text
    for name, pattern in PATTERNS:
        if not pattern.search(redacted):
            continue

        def _replace(match: re.Match[str], rule: str = name) -> str:
            return match.group(0) if _is_placeholder(match.group(0)) else REDACTED

        redacted = pattern.sub(_replace, redacted)
    return redacted
