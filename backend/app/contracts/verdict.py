"""Verdict - the Planner's evaluation of the current article state.

A Verdict is advice, not a decision: the loop controller combines it with budget,
erosion and error rules to emit a :class:`~app.contracts.loop.LoopDecision`.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.contracts.common import NonEmptyStr, VersionedContract
from app.contracts.enums import Recommendation

__all__ = ["Verdict"]


class Verdict(VersionedContract):
    """``meets_minimum`` is the Planner's own bar; the rubric's gates are the real one."""

    meets_minimum: bool
    missing: list[NonEmptyStr] = Field(
        default_factory=list, max_length=40, description="What still has to be true."
    )
    risks: list[NonEmptyStr] = Field(
        default_factory=list, max_length=40, description="What could go wrong if we finish now."
    )
    recommendation: Recommendation

    @model_validator(mode="after")
    def _recommendation_matches_minimum(self) -> Verdict:
        needs_target = (
            self.meets_minimum
            and self.recommendation is Recommendation.CONTINUE
            and not self.missing
        )
        if needs_target:
            raise ValueError(
                "recommendation=continue with meets_minimum=true needs at least one entry in "
                "missing; otherwise the loop would iterate without a target"
            )
        if not self.meets_minimum and self.recommendation is Recommendation.FINISH:
            raise ValueError(
                "recommendation=finish contradicts meets_minimum=false; the controller would "
                "be asked to accept a sub-standard article"
            )
        return self
