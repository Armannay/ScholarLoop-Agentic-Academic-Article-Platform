"""LoopDecision - the only thing the loop controller may emit.

Deterministic and explainable: ``reasons`` are stable snake_case codes (so they can
be counted and charted) and ``evidence`` carries the numbers behind the decision.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from app.contracts.common import VersionedContract
from app.contracts.enums import LoopAction

__all__ = ["STOP_ACTIONS", "LoopDecision"]

#: Actions that end the loop. Everything else means "run another iteration".
STOP_ACTIONS: frozenset[LoopAction] = frozenset(
    {
        LoopAction.STOP_EROSION,
        LoopAction.STOP_BUDGET,
        LoopAction.STOP_ERRORS,
        LoopAction.STOP_USER,
    }
)


class LoopDecision(VersionedContract):
    """One decision by the controller: what to do next, and why."""

    iter: int = Field(ge=0, le=10_000)
    action: LoopAction
    reasons: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Stable snake_case codes, never free prose (e.g. 'plateau_detected').",
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Numbers behind the call: Q history, tokens spent, failed gates.",
    )

    @model_validator(mode="after")
    def _reasons_are_codes(self) -> LoopDecision:
        for reason in self.reasons:
            if reason != reason.strip() or " " in reason or reason != reason.lower():
                raise ValueError(f"reason {reason!r} must be a lower_snake_case code, not prose")
        return self

    @property
    def is_terminal(self) -> bool:
        """True when this decision ends the loop."""
        return self.action in STOP_ACTIONS

    @property
    def needs_planner(self) -> bool:
        """True when the next step is a Planner call (continue or replan)."""
        return self.action in {LoopAction.CONTINUE, LoopAction.OFFER_FINISH}
