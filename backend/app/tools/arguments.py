"""Argument validation for tools.

Tool arguments are model output, so they are untrusted input: every value is checked
against the same rules the tool's JSON schema advertises. Errors name the *field* and
the *rule*, never the value - a malformed argument must not be able to smuggle an API
key into a log line or a ledger entry (HARD RULE 2).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from app.tools.errors import ToolArgumentError

__all__ = [
    "optional_int",
    "optional_string",
    "reject_unknown_arguments",
    "require_string",
]


def reject_unknown_arguments(arguments: Mapping[str, Any], allowed: Collection[str]) -> None:
    """Fail on fields the schema did not declare (``additionalProperties: false``)."""
    unknown = sorted(str(key) for key in arguments if key not in allowed)
    if unknown:
        raise ToolArgumentError(f"unknown argument(s): {', '.join(unknown)}")


def require_string(arguments: Mapping[str, Any], name: str, *, max_length: int) -> str:
    """Return a non-blank string argument of at most ``max_length`` characters."""
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"{name} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ToolArgumentError(f"{name} must be at most {max_length} characters")
    return cleaned


def optional_string(arguments: Mapping[str, Any], name: str, *, max_length: int) -> str | None:
    """Return an optional non-blank string argument, or ``None`` when absent."""
    if name not in arguments or arguments[name] is None:
        return None
    return require_string(arguments, name, max_length=max_length)


def optional_int(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Return a bounded integer argument; booleans and floats are rejected."""
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ToolArgumentError(f"{name} must be between {minimum} and {maximum}")
    return value
