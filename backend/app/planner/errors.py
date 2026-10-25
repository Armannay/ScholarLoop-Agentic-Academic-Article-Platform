"""Planner failures - the only errors this package raises.

The Planner is not an agent: it plans and evaluates, it never writes article text. Its
failures therefore fall into three families, and each maps to a different orchestration
decision:

* :class:`PlannerUnavailable` - no usable ``planner`` binding/account. The user has to
  act, so the loop turns this into ``needs_user_input``.
* :class:`PlannerOutputInvalid` - the provider answered something that is not a JSON
  object at all. The loop treats it like any other bad provider response.
* :class:`PlanRejected` / :class:`EvaluationRejected` - the answer parsed but failed
  deterministic validation after the retry budget was spent. Carrying the validator's
  error list (not a prose message) is what makes those retries auditable.

Nothing here holds or echoes a key, a prompt or article text.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.contracts.ledger import TokenUsage
from app.core.errors import ScholarLoopError

__all__ = [
    "EvaluationRejected",
    "PlanRejected",
    "PlannerError",
    "PlannerOutputInvalid",
    "PlannerRejected",
    "PlannerStateMissing",
    "PlannerUnavailable",
]


class PlannerError(ScholarLoopError):
    """Base class for every planner-boundary failure."""

    code = "planner_error"
    http_status = 500


class PlannerUnavailable(PlannerError):
    """No active ``planner`` binding: the user has not configured a key for it.

    A user condition (bindings resolve at call time), so the loop asks for input
    instead of retrying.
    """

    code = "planner_unavailable"
    http_status = 409


class PlannerStateMissing(PlannerError):
    """A planner call needs run state the service was never given (e.g. no brief)."""

    code = "planner_state_missing"
    http_status = 409


class PlannerOutputInvalid(PlannerError):
    """The provider reply was not one JSON object; nothing could be validated."""

    code = "planner_output_invalid"
    http_status = 502


class PlannerRejected(PlannerError):
    """Output parsed but deterministic validation failed after the retry budget.

    ``problems`` is exactly the list the last retry received: a retry gets the
    validator's error list and nothing else that the rejected answer contained.
    """

    code = "planner_rejected"
    http_status = 422

    def __init__(
        self,
        message: str,
        *,
        problems: Sequence[str],
        attempts: int,
        tokens: TokenUsage,
    ) -> None:
        self.problems: tuple[str, ...] = tuple(problems) or ("invalid output",)
        self.attempts = attempts
        self.tokens = tokens
        super().__init__(
            message,
            problems=list(self.problems),
            attempts=attempts,
            tokens=tokens.model_dump(mode="json"),
        )


class PlanRejected(PlannerRejected):
    """A plan draft could not be turned into a contract-valid :class:`Plan`."""

    code = "plan_rejected"


class EvaluationRejected(PlannerRejected):
    """An evaluation draft could not be turned into a contract-valid :class:`Verdict`."""

    code = "evaluation_rejected"
