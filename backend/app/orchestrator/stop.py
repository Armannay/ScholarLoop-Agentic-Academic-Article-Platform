"""Stop handling: the ``StopReport`` and the completion checkpoint options.

Step 9a, item 5. Whenever the loop ends - completion offered, erosion,
budget, errors or the user - the orchestrator snapshots the best version it
measured and emits one :class:`StopReport`: why it stopped, the evidence,
the full Q series, which snapshot is best, and what the user may do next
(``rollback``, ``edit_goals``, ``add_budget``, ``continue_anyway``, and at a
completion checkpoint ``finish`` / ``continue``).

The completion checkpoint behaviour is ``on_complete``:

* ``ask`` (default) - pause in ``CHECKPOINT``; the user picks Finish or
  Continue (optionally with extra budget or a new goal).
* ``finish`` - finish immediately, no pause.
* ``continue_once`` - run exactly one more iteration, then ask.

Everything here is a pure value or a pure builder; the orchestrator owns the
side effects (snapshotting, restoring, resuming).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.contracts.common import Contract, NonEmptyStr, Ulid, VersionedContract
from app.contracts.enums import LoopAction
from app.contracts.loop import LoopDecision
from app.contracts.verdict import Verdict
from app.core.clock import utc_now
from app.orchestrator.loop_controller import LoopHistory

__all__ = [
    "DEFAULT_ON_COMPLETE",
    "OnComplete",
    "StopOption",
    "StopReport",
    "UserDecision",
    "build_stop_report",
    "options_for",
]


class OnComplete(StrEnum):
    """What happens when the loop reaches a completion candidate."""

    ASK = "ask"
    FINISH = "finish"
    CONTINUE_ONCE = "continue_once"


#: The shipped checkpoint behaviour: never finish silently.
DEFAULT_ON_COMPLETE: OnComplete = OnComplete.ASK


class StopOption(StrEnum):
    """What the user may choose after a stop or at a checkpoint."""

    FINISH = "finish"
    CONTINUE = "continue"
    ROLLBACK = "rollback"
    EDIT_GOALS = "edit_goals"
    ADD_BUDGET = "add_budget"
    CONTINUE_ANYWAY = "continue_anyway"


def options_for(action: LoopAction, *, has_best_snapshot: bool) -> list[StopOption]:
    """The options that make sense for ``action``. Deterministic order."""
    rollback = [StopOption.ROLLBACK] if has_best_snapshot else []
    if action is LoopAction.OFFER_FINISH:
        return [StopOption.FINISH, StopOption.CONTINUE, *rollback]
    if action is LoopAction.STOP_BUDGET:
        return [StopOption.ADD_BUDGET, StopOption.EDIT_GOALS, *rollback, StopOption.CONTINUE_ANYWAY]
    if action in {LoopAction.STOP_EROSION, LoopAction.STOP_ERRORS}:
        return [*rollback, StopOption.EDIT_GOALS, StopOption.ADD_BUDGET, StopOption.CONTINUE_ANYWAY]
    if action is LoopAction.STOP_USER:
        return [StopOption.CONTINUE_ANYWAY, StopOption.EDIT_GOALS, *rollback]
    return [StopOption.CONTINUE, *rollback]


class StopReport(VersionedContract):
    """Why the loop stopped, with the numbers and the user's options."""

    run_id: Ulid
    action: LoopAction
    reasons: list[str] = Field(min_length=1, max_length=20)
    evidence: dict[str, Any] = Field(default_factory=dict)
    q_series: list[int] = Field(default_factory=list)
    current_q: int | None = None
    best_q: float | None = None
    best_iter: int | None = None
    best_snapshot_id: str | None = None
    iterations: int = Field(default=0, ge=0)
    tokens_spent: int = Field(default=0, ge=0)
    verdict: Verdict | None = None
    options: list[StopOption] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


def build_stop_report(
    *,
    run_id: str,
    decision: LoopDecision,
    history: LoopHistory,
    best_snapshot_id: str | None,
    verdict: Verdict | None = None,
    created_at: datetime | None = None,
) -> StopReport:
    """Assemble the report for a terminal/checkpoint ``decision`` (pure)."""
    latest = history.latest_report
    best_q = history.best_q()
    return StopReport(
        run_id=run_id,
        action=decision.action,
        reasons=list(decision.reasons),
        evidence=dict(decision.evidence),
        q_series=history.q_series,
        current_q=latest.Q if latest is not None else None,
        best_q=None if best_q is None else float(best_q),
        best_iter=history.best_iter(),
        best_snapshot_id=best_snapshot_id,
        iterations=history.current_iter,
        tokens_spent=history.resolved_tokens_spent(),
        verdict=verdict,
        options=options_for(decision.action, has_best_snapshot=best_snapshot_id is not None),
        created_at=created_at or utc_now(),
    )


class UserDecision(Contract):
    """The user's answer at a checkpoint or after a stop.

    ``extra_tokens`` / ``extra_iterations`` extend the caps for
    ``add_budget`` and ``continue``; ``new_goal`` replaces the run goal for
    ``edit_goals`` (and may accompany ``continue``); ``note`` carries the
    information an agent asked for when the checkpoint kind is
    ``needs_input`` - it is passed to the Planner as user-supplied input,
    never blended into article text by the orchestrator itself.
    """

    option: StopOption
    extra_tokens: int | None = Field(default=None, ge=1, le=100_000_000)
    extra_iterations: int | None = Field(default=None, ge=1, le=200)
    new_goal: NonEmptyStr | None = Field(default=None, max_length=800)
    note: NonEmptyStr | None = Field(default=None, max_length=4000)
    wait: bool = Field(
        default=False, description="API convenience: run inline until the next pause/stop."
    )
