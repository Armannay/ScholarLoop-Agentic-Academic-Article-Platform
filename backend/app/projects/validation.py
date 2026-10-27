"""Project payload validation: the *frozen contracts* decide, not this module.

A project stores two blobs whose shape is already owned somewhere else:

* ``brief`` - a CONTRACTS v1 :class:`~app.contracts.brief.Brief`. Validated by the
  contract itself, so a new ``Brief`` rule is enforced here the day it lands (the
  contracts are frozen, but the loader reads them, it does not copy them).
* ``policy_overrides`` - a partial policy block, deep-merged over the code defaults
  and validated by :class:`~app.core.policies.Policies` through
  :func:`app.core.policies.deep_merge`. An unknown key is an error, exactly as it is
  in ``config/policies.yaml`` - a mistyped override that silently kept the default
  would be worse than a rejected request.

Failures become :class:`ProjectValidationError` (422) carrying a **per-field** list:
``[{"loc": "brief.target_words", "msg": "...", "type": "..."}]``. The submitted value
is never echoed - the same rule the API's request-validation handler follows, so a
brief cannot smuggle user text (or a pasted credential) into a response or a log line.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from app.contracts.brief import Brief
from app.core.errors import ConfigError, ScholarLoopError
from app.core.policies import DEFAULT_POLICIES, Policies, deep_merge
from app.core.redaction import REDACTED, redact

__all__ = [
    "ProjectValidationError",
    "policy_override_keys",
    "validate_brief",
    "validate_policy_overrides",
]

_UNKNOWN_KEY_RE = re.compile(r"unknown policy key '([^']+)'")
_BRIEF_PREFIX = "brief"
_POLICY_PREFIX = "policy_overrides"
#: Shorter strings are not scrubbed: a model-level pydantic message can legitimately
#: contain short words like "en" or "research", and mangling it would help nobody.
_SCRUB_MIN_LENGTH = 8


class ProjectValidationError(ScholarLoopError):
    """A project payload is structurally valid JSON but invalid for its contract.

    ``context.errors`` is the machine-readable field list; ``message`` is a four-field
    summary for humans. Both are derived from pydantic's error records and contain no
    submitted values.
    """

    code = "validation_error"
    http_status = 422

    def __init__(self, errors: list[dict[str, str]], *, message: str | None = None) -> None:
        summary = "; ".join(f"{item['loc']}: {item['msg']}" for item in errors[:4])
        remaining = len(errors) - 4
        if remaining > 0:
            summary += f" (+{remaining} more)"
        super().__init__(message or summary or "invalid project payload", errors=errors)


def _submitted_strings(payload: Any) -> list[str]:
    """Every string in a submitted JSON document, longest first.

    Used to scrub error *messages*: pydantic's cross-field validators compose their own
    text and can quote the offending values (``must_include and must_avoid overlap,
    so the brief is unsatisfiable: ['...']``). A rejection must not hand user text -
    possibly a pasted credential - back in the response or into a log line.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
        elif isinstance(node, str) and len(node) >= _SCRUB_MIN_LENGTH:
            found.add(node)

    walk(payload)
    return sorted(found, key=len, reverse=True)


def _scrub(message: str, values: Sequence[str]) -> str:
    """Redact secret shapes, then any submitted string the message quotes."""
    scrubbed = redact(message)
    for value in values:
        if value in scrubbed:
            scrubbed = scrubbed.replace(value, REDACTED)
    return scrubbed


def _field_errors(
    exc: ValidationError, *, prefix: str, values: Sequence[str] = ()
) -> list[dict[str, str]]:
    """Convert pydantic errors into ``{loc, msg, type}`` records under ``prefix``.

    ``input``, ``ctx`` and ``url`` are dropped deliberately: the response must stay
    free of submitted values, and an exception's ``ctx`` can hold an object whose
    repr is not safe to serve. ``values`` additionally scrubs the message itself.
    """
    errors: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        errors.append(
            {
                "loc": f"{prefix}.{location}" if location else prefix,
                "msg": _scrub(str(error.get("msg", "invalid")), values),
                "type": str(error.get("type", "value_error")),
            }
        )
    return errors


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    """Reject a JSON array/string where the database column expects an object."""
    if not isinstance(value, Mapping):
        raise ProjectValidationError(
            [{"loc": field, "msg": "must be a JSON object", "type": "type_error"}]
        )
    return value


def validate_brief(payload: Any) -> dict[str, Any]:
    """Validate a raw brief against the frozen contract and return it JSON-safe.

    Args:
        payload: the request body's ``brief`` member.

    Returns:
        ``Brief.model_dump(mode="json")`` - what is stored in ``projects.brief``.

    Raises:
        ProjectValidationError: 422 with one entry per offending brief field.
    """
    document = _require_mapping(payload, field=_BRIEF_PREFIX)
    try:
        brief = Brief.model_validate(dict(document))
    except ValidationError as exc:
        errors = _field_errors(exc, prefix=_BRIEF_PREFIX, values=_submitted_strings(dict(document)))
        summary = "; ".join(f"{item['loc']}: {item['msg']}" for item in errors[:4])
        raise ProjectValidationError(errors, message=f"invalid brief: {summary}") from exc
    return brief.model_dump(mode="json")


def validate_policy_overrides(overrides: Any) -> dict[str, Any]:
    """Validate a partial policy block and return exactly what should be stored.

    The override is merged over :data:`app.core.policies.DEFAULT_POLICIES` with
    :func:`app.core.policies.deep_merge` (unknown keys rejected) and then validated by
    :class:`~app.core.policies.Policies` (ranges, nesting rules). Only the caller's
    overrides are stored - the derived full policy set stays derived.

    Raises:
        ProjectValidationError: 422 naming the offending policy key or value.
    """
    if overrides is None:
        return {}
    document = _require_mapping(overrides, field=_POLICY_PREFIX)
    if not document:
        return {}
    try:
        merged = deep_merge(
            DEFAULT_POLICIES.model_dump(mode="json"),
            dict(document),
            where=_POLICY_PREFIX,
        )
        Policies.model_validate(merged)
    except ConfigError as exc:
        # deep_merge names the exact dotted key; reuse it as the error location.
        match = _UNKNOWN_KEY_RE.search(exc.message)
        location = match.group(1) if match is not None else _POLICY_PREFIX
        raise ProjectValidationError(
            [{"loc": location, "msg": exc.message, "type": "unknown_key"}]
        ) from exc
    except ValidationError as exc:
        raise ProjectValidationError(
            _field_errors(exc, prefix=_POLICY_PREFIX, values=_submitted_strings(dict(document)))
        ) from exc
    return dict(document)


def policy_override_keys(overrides: Mapping[str, Any] | None) -> list[str]:
    """Sorted top-level keys of an override block - for audit detail, never values.

    The audit trail records *which* policies a user changed, not the numbers: the
    detail column is a log sink, and a project's policy block must not become a second,
    unredacted copy of user input.
    """
    return sorted(str(key) for key in (overrides or {}))
