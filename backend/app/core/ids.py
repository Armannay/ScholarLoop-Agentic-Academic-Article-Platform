"""ULID generation and validation.

Dependency-free, 26-character Crockford base32: 48-bit millisecond timestamp +
80-bit randomness. Ids are lexicographically sortable by creation time, which is
what makes the append-only ledger cheap to scan and cheap to digest.

Determinism is a feature here: every factory accepts an injected timestamp and
entropy source, so tests get byte-identical ids without freezing global state.
"""

from __future__ import annotations

import os
import time
from typing import Final

CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH: Final[int] = 26
_TIME_CHARS: Final[int] = 10
_RANDOM_CHARS: Final[int] = 16
_RANDOM_BITS: Final[int] = 80
_MAX_TIME_MS: Final[int] = (1 << 48) - 1
_MAX_RANDOM: Final[int] = (1 << _RANDOM_BITS) - 1

_DECODE: Final[dict[str, int]] = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}
# Crockford decoding tolerates these look-alikes so ids survive being typed by hand.
for _alias, _canonical in (("O", "0"), ("o", "0"), ("I", "1"), ("i", "1"), ("L", "1")):
    _DECODE[_alias] = _DECODE[_canonical]


def _encode(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters (most significant first)."""
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _decode(text: str) -> int:
    value = 0
    for char in text:
        try:
            value = (value << 5) | _DECODE[char]
        except KeyError as exc:  # pragma: no cover - guarded by is_ulid/new_ulid callers
            raise ValueError(f"illegal Crockford base32 character: {char!r}") from exc
    return value


def time_to_ulid_prefix(timestamp_ms: int) -> str:
    """Encode a millisecond timestamp as the 10-character ULID prefix."""
    if not 0 <= timestamp_ms <= _MAX_TIME_MS:
        raise ValueError("timestamp out of ULID range (0..2**48-1 ms)")
    return _encode(timestamp_ms, _TIME_CHARS)


def ulid_timestamp_ms(value: str) -> int:
    """Extract the creation timestamp (ms since epoch) from a ULID."""
    if not is_ulid(value):
        raise ValueError(f"not a ULID: {value!r}")
    return _decode(value[:_TIME_CHARS])


def new_ulid(*, timestamp_ms: int | None = None, entropy: bytes | None = None) -> str:
    """Build a ULID.

    Args:
        timestamp_ms: milliseconds since the Unix epoch; defaults to now.
        entropy: exactly 10 bytes of randomness; defaults to ``os.urandom(10)``.
    """
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if entropy is None:
        entropy = os.urandom(10)
    if len(entropy) != 10:
        raise ValueError("entropy must be exactly 10 bytes (80 bits)")
    prefix = time_to_ulid_prefix(timestamp_ms)
    return prefix + _encode(int.from_bytes(entropy, "big"), _RANDOM_CHARS)


def is_ulid(value: object) -> bool:
    """True for a well-formed, correctly-sized ULID string."""
    if not isinstance(value, str) or len(value) != ULID_LENGTH:
        return False
    if value[0] not in "01234567":  # 48-bit time; the first char cannot overflow
        return False
    return all(char in _DECODE for char in value)


class UlidFactory:
    """Monotonic ULID factory: ids created in the same millisecond keep increasing.

    The random component is incremented rather than re-drawn, so a burst of ids
    inside one millisecond is strictly ordered and still unique.
    """

    def __init__(self, *, timestamp_ms: int | None = None, entropy: bytes | None = None) -> None:
        if entropy is not None and len(entropy) != 10:
            raise ValueError("entropy must be exactly 10 bytes (80 bits)")
        self._last_ms: int = timestamp_ms if timestamp_ms is not None else -1
        self._last_random: int = int.from_bytes(entropy, "big") if entropy is not None else -1
        self._entropy_source = entropy

    def __call__(self) -> str:
        now_ms = self._last_ms if self._entropy_source is not None else time.time_ns() // 1_000_000
        if now_ms == self._last_ms:
            self._last_random = (self._last_random + 1) & _MAX_RANDOM
        else:
            self._last_ms = now_ms
            self._last_random = int.from_bytes(os.urandom(10), "big")
        return time_to_ulid_prefix(self._last_ms) + _encode(self._last_random, _RANDOM_CHARS)
