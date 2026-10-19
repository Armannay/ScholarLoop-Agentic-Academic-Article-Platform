"""Planner errors - the only failures the planner layer raises of its own.

Provider failures are *not* wrapped here: a :class:`~app.providers.errors.ProviderError`
keeps its ``retryable`` fact and account context, so the orchestrator and the error
streak guard decide what to do. These errors cover the planner's own domain: a model
reply that is still invalid after the bounded repair loop, and a prompt that would
exceed the policy caps (which the planner refuses instead of silently truncating).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.errors import ScholarLoopError

__all__ = [
    "MAX_BODY_PREVIEW_CHARS",
    "PlannerContextTooLarge",
    "PlannerError",
    "PlannerOutputError",
]

#: A repair-failure error names what was wrong; it does not smuggle the article-sized
#: model reply into logs. Two hundred characters identify the reply without copying it.
MAX_BODY_PREVIEW_CHARS: int = 200


class PlannerError(ScholarLoopError):
    """Base class for every planner-layer failure."""

    code = "planner_error"
    http_status = 500


class PlannerOutputError(PlannerError):
    """The model's reply was still not a valid contract after every repair attempt.

    ``issues`` are the machine-readable validation complaints from the last attempt,
    in the order the repair prompt listed them. The orchestrator treats this as a
    failed call (it counts toward ``error_streak``), never as a plan.
    """

    code = "planner_output_invalid"
    http_status = 422

    def __init__(
        self,
        message: str,
        *,
        issues: Sequence[str],
        attempts: int,
        body_preview: str | None = None,
    ) -> None:
        preview = body_preview[:MAX_BODY_PREVIEW_CHARS] if body_preview else None
        super().__init__(
            message,
            issues=list(issues),
            attempts=attempts,
            body_preview=preview,
        )
        self.issues: tuple[str, ...] = tuple(issues)
        self.attempts: int = attempts
        self.body_preview: str | None = preview


class PlannerContextTooLarge(PlannerError):
    """The assembled prompt would exceed a policy cap; nothing was sent to a provider.

    ``parts`` breaks the estimate down by component (digest, brief, system, ...) so
    the caller knows *what* to shrink - compact the digest, drop source references -
    instead of retrying blindly. Refusing here is HARD RULE 3: a truncated digest
    would silently change what the Planner believes happened.
    """

    code = "planner_context_too_large"
    http_status = 413

    def __init__(
        self,
        message: str,
        *,
        tokens: int,
        cap: int,
        parts: Mapping[str, int],
    ) -> None:
        super().__init__(message, tokens=tokens, cap=cap, parts=dict(parts))
        self.tokens: int = tokens
        self.cap: int = cap
        self.parts: dict[str, int] = dict(parts)
