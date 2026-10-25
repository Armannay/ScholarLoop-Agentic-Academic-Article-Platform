"""When to call the Planner - the event policy, not a timer.

The Planner is the most expensive role per call, so it is called at *events*, never on a
schedule and never twice for the same event:

===============  ===========================================  ==========================
event            when                                         strategy_change
===============  ===========================================  ==========================
RUN_START        once, and only while no plan exists          no
QUEUE_EMPTY      the task queue emptied without completion    no
GATE_NEWLY_FAILED a hard gate that was passing (or unknown)   no
                 has started to fail
MILESTONE_COMPLETE one named milestone finished, once each    no
FIRST_PLATEAU    the loop's first plateau                     **yes**
===============  ===========================================  ==========================

Two global guards apply to every event: at most one planner call per iteration, and an
optional minimum gap between calls (:attr:`PlannerCallPolicy.min_iteration_gap`). Both
exist because a loop that calls the Planner twice in one iteration has already lost the
argument that planning is cheap.

The policy is stateful only about what it has already seen (called iterations, known
gate failures, milestones, the plateau). It never decides *what* to plan: it answers
"should there be a planner call now, and why", and the orchestrator acts on the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from app.contracts.enums import LoopAction
from app.contracts.loop import LoopDecision

__all__ = ["PLATEAU_REASON", "PlannerCall", "PlannerCallPolicy", "PlannerEvent"]

#: The loop controller's reason code for a plateau (``LoopDecision.reasons``).
PLATEAU_REASON: Final[str] = "plateau_detected"


class PlannerEvent(StrEnum):
    """The five orchestration events that may justify a planner call."""

    RUN_START = "run_start"
    QUEUE_EMPTY = "queue_empty"
    GATE_NEWLY_FAILED = "gate_newly_failed"
    MILESTONE_COMPLETE = "milestone_complete"
    FIRST_PLATEAU = "first_plateau"


@dataclass(frozen=True)
class PlannerCall:
    """A granted call: the event, the iteration and the reason code for the ledger."""

    event: PlannerEvent
    iteration: int
    strategy_change: bool
    reason: str


@dataclass
class PlannerCallPolicy:
    """Stateful, deterministic gate in front of :meth:`PlannerService.plan`.

    Args:
        min_iteration_gap: minimum iterations between two planner calls (0 = only the
            one-call-per-iteration guard applies).
    """

    min_iteration_gap: int = 0
    _last_call_iteration: int | None = field(default=None, init=False)
    _known_failed_gates: set[str] = field(default_factory=set, init=False)
    _milestones_seen: set[str] = field(default_factory=set, init=False)
    _plateau_handled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.min_iteration_gap < 0:
            raise ValueError(f"min_iteration_gap must be >= 0, got {self.min_iteration_gap}")

    # -- introspection -----------------------------------------------------
    @property
    def last_call_iteration(self) -> int | None:
        """Iteration of the last granted call, or ``None``."""
        return self._last_call_iteration

    @property
    def known_failed_gates(self) -> frozenset[str]:
        """Gate names the policy already knows are failing."""
        return frozenset(self._known_failed_gates)

    @property
    def plateau_handled(self) -> bool:
        """True once the first plateau bought its one strategy change."""
        return self._plateau_handled

    # -- policy ------------------------------------------------------------
    def should_call(
        self,
        event: PlannerEvent,
        *,
        iteration: int,
        plan_version: int | None = None,
        failed_gates: tuple[str, ...] | list[str] = (),
        milestone: str | None = None,
        plateau_count: int = 0,
    ) -> PlannerCall | None:
        """Return the granted call, or ``None`` when this event must not spend tokens.

        Args:
            event: the orchestration event being considered.
            iteration: the loop iteration it happened in (one call per iteration).
            plan_version: the current plan version; ``None`` means no plan yet.
            failed_gates: hard gates currently failing, for the newly-failed test.
            milestone: a stable milestone id, for once-per-milestone delivery.
            plateau_count: how many plateaus the loop has recorded; only the first one
                is a planning event.
        """
        if not 0 <= iteration <= 10_000:
            raise ValueError(f"iteration must be 0..10000, got {iteration}")
        if self._last_call_iteration is not None:
            if iteration <= self._last_call_iteration:
                return None
            if iteration - self._last_call_iteration <= self.min_iteration_gap:
                return None

        newly_failed = sorted(set(failed_gates) - self._known_failed_gates)
        if event is PlannerEvent.RUN_START:
            if plan_version is not None:
                return None
            return self._grant(PlannerEvent.RUN_START, iteration, "no_plan_yet")
        if event is PlannerEvent.QUEUE_EMPTY:
            return self._grant(PlannerEvent.QUEUE_EMPTY, iteration, "task_queue_empty")
        if event is PlannerEvent.GATE_NEWLY_FAILED:
            if not newly_failed:
                return None
            return self._grant(
                PlannerEvent.GATE_NEWLY_FAILED,
                iteration,
                f"gate_newly_failed:{newly_failed[0]}",
            )
        if event is PlannerEvent.MILESTONE_COMPLETE:
            if milestone is None or milestone in self._milestones_seen:
                return None
            return self._grant(PlannerEvent.MILESTONE_COMPLETE, iteration, f"milestone:{milestone}")
        if event is PlannerEvent.FIRST_PLATEAU:
            if self._plateau_handled or plateau_count != 1:
                return None
            return self._grant(
                PlannerEvent.FIRST_PLATEAU,
                iteration,
                PLATEAU_REASON,
                strategy_change=True,
            )
        raise AssertionError(f"unhandled planner event {event!r}")  # pragma: no cover

    def observe_failed_gates(self, failed_gates: tuple[str, ...] | list[str]) -> None:
        """Record gate failures without granting a call.

        The orchestrator calls this after every probe run, so the next
        ``GATE_NEWLY_FAILED`` means a gate *became* failing rather than "is still failing".
        """
        self._known_failed_gates.update(failed_gates)

    def from_decision(
        self,
        decision: LoopDecision,
        *,
        plan_version: int | None = None,
        failed_gates: tuple[str, ...] | list[str] = (),
        milestone: str | None = None,
        queue_empty: bool = False,
    ) -> PlannerCall | None:
        """Map a loop decision to a planner event, then apply the policy.

        Only ``CONTINUE`` can produce a call: every stop action (budget, errors, erosion,
        user) ends the run, and ``OFFER_FINISH``/``ASK_USER`` are already waiting on a
        human. The event itself is chosen from the decision's stable reason codes:
        a plateau wins over an empty queue, which wins over a newly failing gate.
        """
        if decision.action is not LoopAction.CONTINUE:
            return None
        if PLATEAU_REASON in decision.reasons:
            plateau_count = _int_evidence(decision.evidence, "plateau_count", default=1)
            return self.should_call(
                PlannerEvent.FIRST_PLATEAU,
                iteration=decision.iter,
                plan_version=plan_version,
                failed_gates=failed_gates,
                plateau_count=plateau_count,
            )
        if queue_empty:
            return self.should_call(
                PlannerEvent.QUEUE_EMPTY,
                iteration=decision.iter,
                plan_version=plan_version,
                failed_gates=failed_gates,
            )
        if set(failed_gates) - self._known_failed_gates:
            return self.should_call(
                PlannerEvent.GATE_NEWLY_FAILED,
                iteration=decision.iter,
                plan_version=plan_version,
                failed_gates=failed_gates,
            )
        return None

    # -- internals ---------------------------------------------------------
    def _grant(
        self,
        event: PlannerEvent,
        iteration: int,
        reason: str,
        *,
        strategy_change: bool = False,
    ) -> PlannerCall:
        """Record the grant and update the event-specific state."""
        self._last_call_iteration = iteration
        if event is PlannerEvent.GATE_NEWLY_FAILED:
            # Gate state is refreshed by observe_failed_gates; a granted call still marks
            # the failure as seen so the same failure cannot buy a second call.
            self._known_failed_gates.add(reason.removeprefix("gate_newly_failed:"))
        if event is PlannerEvent.MILESTONE_COMPLETE:
            self._milestones_seen.add(reason.removeprefix("milestone:"))
        if event is PlannerEvent.FIRST_PLATEAU:
            self._plateau_handled = True
        return PlannerCall(
            event=event,
            iteration=iteration,
            strategy_change=strategy_change,
            reason=reason,
        )


def _int_evidence(evidence: dict[str, Any], key: str, *, default: int) -> int:
    """Read an integer from ``LoopDecision.evidence`` without trusting its type."""
    value = evidence.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value
