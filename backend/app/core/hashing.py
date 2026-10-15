"""Deterministic content hashing.

Section text is stored once; everything downstream references it by hash, which is
how the architecture stays token-frugal (pass references, not content) while still
detecting stale patches: a patch names the ``base_version`` it was built against
and the section carries the hash of the exact bytes it was written against.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = ["ContentHash", "content_hash", "hash_matches", "normalise_text"]

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: ``sha256:<64 hex>`` - the only hash spelling accepted by the contracts.
ContentHash = str


def normalise_text(text: str) -> str:
    """Canonicalise before hashing so trivially different bytes hash the same.

    Unicode NFC + CRLF->LF + trailing-whitespace trimming. Interior whitespace is
    preserved: reflowing a paragraph is a real change and must invalidate a patch.
    """
    canonical = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in canonical.split("\n")]
    return "\n".join(lines).strip("\n")


def content_hash(text: str) -> str:
    """Hash of ``text`` in ``sha256:<hex>`` form."""
    digest = hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def hash_matches(value: str, expected: str) -> bool:
    """Constant-shape comparison of two hashes, both validated for spelling first."""
    return _HASH_RE.match(value) is not None and value == expected
