"""LoopController - the deterministic half of the loop (no LLM in this path).

The controller combines, in a fixed precedence, the numbers of one finished
iteration into exactly one :class:`~app.contracts.loop.LoopDecision`:

1. ``STOP_USER`` - the user asked to stop; nothing overrules a human.
2. ``ASK_USER`` - an agent returned ``needs_user_input`` (HARD RULE 1). Pausing
   beats every stop below it: the answer may unblock the run.
3. ``STOP_ERRORS`` - the consecutive-failure streak reached ``error_streak``.
4. ``OFFER_FINISH`` - every hard gate passes, ``Q >= q_min``, no high-severity
   issue is open, and the Planner does not report missing minimum work. An
   acceptable article is *offered* even when budget or erosion would also
   fire, and the firing rule is recorded in ``reasons`` alongside
   ``quality_acceptable`` so the offer stays explainable.
5. ``STOP_BUDGET`` - run/iteration token cap reached, or ``max_iterations``.
6. ``STOP_EROSION`` - any erosion detector fired (below).
7. ``CONTINUE`` - healthy but unfinished; the reasons say exactly why.

The Planner's ``Verdict`` is advice, never a decision: it can delay an offer
(it reports missing minimum work) but it can never create one - only the
measured gates and Q can. Every detector is a pure function over the Q/token
history so the whole decision is reproducible from ``LoopDecision.evidence``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from app.contracts.enums import LoopAction, Recommendation
from app.contracts.loop import LoopDecision
from app.contracts.quality import QualityReport
from app.contracts.verdict import Verdict
from app.core.policies import DEFAULT_POLICIES, DiminishingPolicy, Policies

__all__ = [
    "LoopController",
    "LoopState",
    "churn_detected",
    "detect_erosion",
    "diminishing_detected",
    "oscillation_detected",
    "plateau_detected",
    "regression_detected",
]

#: Churn needs a minimum of opened issues before a ratio means anything: a first
#: review opens issues and closes none by construction, which is not erosion.
MIN_OPENED_FOR_CHURN = 3
#: Churn also needs at least this many measured iterations, so closing has had
#: a chance to happen before the loop is blamed for not closing.
MIN_MEASURED_FOR_CHURN = 3


@dataclass(frozen=True)
class LoopState:
    """Everything one decision may look at - numbers and codes, never text."""

    iteration: int
    q_history: tuple[float, ...] = ()
    #: Tokens spent per finished iteration, aligned so the last entry is the
    #: iteration that produced the last Q (planner calls included).
    iteration_tokens: tuple[int, ...] = ()
    run_tokens: int = 0
    report: QualityReport | None = None
    verdict: Verdict | None = None
    error_streak: int = 0
    issues_opened: int = 0
    issues_closed: int = 0
    #: ``IssueSummary.churn_ratio``: still-open / ever-opened, in 0.0..1.0.
    issue_churn_ratio: float = 0.0
    needs_user_input: bool = False
    user_stop: bool = False
    #: Sections whose head body round-tripped (``article.recurrence``).
    recurred_sections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def quality_acceptable(self) -> bool:
        """True when every gate passes and no high-severity issue is open.

        The ``Q >= q_min`` half of "acceptable" lives in the controller,
        which is the component that owns the policies.
        """
        report = self.report
        return report is not None and report.all_gates_pass and not report.high_severity_issues()


# --------------------------------------------------------------------------- detectors
def plateau_detected(q_history: Sequence[float], *, window: int, min_gain: float) -> bool:
    """Q gained less than ``min_gain`` across the last ``window`` measurements."""
    if window < 2 or len(q_history) < window:
        return False
    return (q_history[-1] - q_history[-window]) < min_gain


def regression_detected(q_history: Sequence[float], *, window: int, delta: float) -> bool:
    """Q fell ``delta`` or more below the best of the preceding ``window`` values."""
    if len(q_history) < 2:
        return False
    lookback = q_history[:-1][-window:] if window >= 1 else q_history[:-1]
    return (max(lookback) - q_history[-1]) >= delta


def oscillation_detected(q_history: Sequence[float], *, window: int) -> bool:
    """Q strictly alternated up/down over the last ``window`` measurements."""
    if window < 3 or len(q_history) < window:
        return False
    recent = q_history[-window:]
    signs: list[int] = []
    for previous, current in pairwise(recent):
        diff = current - previous
        if diff == 0:
            return False
        signs.append(1 if diff > 0 else -1)
    return all(signs[index] != signs[index - 1] for index in range(1, len(signs)))


def diminishing_detected(
    q_history: Sequence[float],
    iteration_tokens: Sequence[int],
    policy: DiminishingPolicy,
) -> bool:
    """Q gain per 10k tokens stayed under the floor for ``window`` iterations."""
    window = policy.window
    if len(q_history) < window + 1 or len(iteration_tokens) < window:
        return False
    gains = list(pairwise(q_history))[-window:]
    spends = iteration_tokens[-window:]
    for (before, after), spent in zip(gains, spends, strict=True):
        if spent <= 0:
            return False  # an unspent iteration says nothing about returns
        if (after - before) / (spent / 10_000) >= policy.min_gain_per_10k:
            return False
    return True


def churn_detected(
    *,
    churn_ratio: float,
    threshold: float,
    total_opened: int,
    measured_iterations: int,
) -> bool:
    """Issues are opened and not closed, once the sample is large enough to judge."""
    return (
        total_opened >= MIN_OPENED_FOR_CHURN
        and measured_iterations >= MIN_MEASURED_FOR_CHURN
        and churn_ratio > threshold
    )


def detect_erosion(state: LoopState, policies: Policies) -> list[str]:
    """The erosion reason codes firing for ``state``, in reporting order."""
    q = state.q_history
    reasons: list[str] = []
    if state.recurred_sections and len(q) >= 2:
        reasons.append("section_hash_recurred")
    if regression_detected(q, window=policies.regression.window, delta=policies.regression.delta):
        reasons.append("quality_regression_detected")
    if plateau_detected(q, window=policies.plateau.window, min_gain=policies.plateau.min_gain):
        reasons.append("plateau_detected")
    if oscillation_detected(q, window=policies.oscillation.window):
        reasons.append("oscillation_detected")
    if diminishing_detected(q, state.iteration_tokens, policies.diminishing):
        reasons.append("diminishing_returns_detected")
    if churn_detected(
        churn_ratio=state.issue_churn_ratio,
        threshold=policies.issue_churn,
        total_opened=state.issues_opened,
        measured_iterations=len(q),
    ):
        reasons.append("issue_churn_detected")
    return reasons


class LoopController:
    """Emits one :class:`LoopDecision` per finished iteration."""

    def __init__(self, policies: Policies = DEFAULT_POLICIES) -> None:
        self._policies = policies

    @property
    def policies(self) -> Policies:
        """The policy set every threshold below comes from."""
        return self._policies

    def decide(self, state: LoopState) -> LoopDecision:
        """Decide the loop's next step from ``state`` (see the module precedence)."""
        policies = self._policies
        report = state.report
        evidence = self._evidence(state)
        erosion = detect_erosion(state, policies)
        budget = self._budget_reasons(state)

        if state.user_stop:
            return self._decision(state, LoopAction.STOP_USER, ["user_requested_stop"], evidence)

        if state.needs_user_input:
            return self._decision(state, LoopAction.ASK_USER, ["needs_user_input"], evidence)

        if state.error_streak >= policies.error_streak:
            return self._decision(state, LoopAction.STOP_ERRORS, ["error_streak_reached"], evidence)

        acceptable = state.quality_acceptable and (
            report is not None and policies.q_min <= report.Q
        )
        planner_wants_more = state.verdict is not None and (
            not state.verdict.meets_minimum
            or state.verdict.recommendation is Recommendation.CONTINUE
        )
        if acceptable and not planner_wants_more:
            return self._decision(state, LoopAction.OFFER_FINISH, ["quality_acceptable"], evidence)
        if acceptable and (budget or erosion):
            # Good enough to offer, out of road to improve: the offer wins over a
            # bare STOP_*, and both facts stay in the reasons.
            return self._decision(
                state,
                LoopAction.OFFER_FINISH,
                ["quality_acceptable", *budget, *erosion],
                evidence,
            )

        if budget:
            return self._decision(state, LoopAction.STOP_BUDGET, budget, evidence)

        if erosion:
            return self._decision(state, LoopAction.STOP_EROSION, erosion, evidence)

        return self._decision(state, LoopAction.CONTINUE, self._continue_reasons(state), evidence)

    # -- internals ------------------------------------------------------------

    def _budget_reasons(self, state: LoopState) -> list[str]:
        """Budget reason codes for the caps this state has reached."""
        policies = self._policies
        reasons: list[str] = []
        if state.run_tokens >= policies.run_token_cap:
            reasons.append("run_token_cap_reached")
        if state.iteration_tokens and state.iteration_tokens[-1] >= policies.iteration_token_cap:
            reasons.append("iteration_token_cap_reached")
        if state.iteration >= policies.max_iterations:
            reasons.append("max_iterations_reached")
        return reasons

    def _continue_reasons(self, state: LoopState) -> list[str]:
        """Why a healthy loop keeps going: the unfinished facts, as codes."""
        report = state.report
        if report is None:
            return ["no_quality_report"]
        reasons: list[str] = []
        if self._policies.q_min > report.Q:
            reasons.append("quality_below_minimum")
        if report.failed_gates:
            reasons.append("gates_failing")
        if report.high_severity_issues():
            reasons.append("high_severity_issues_open")
        if state.verdict is not None and not state.verdict.meets_minimum:
            reasons.append("planner_reports_missing_work")
        elif state.verdict is not None and state.verdict.recommendation is Recommendation.CONTINUE:
            reasons.append("planner_recommends_continue")
        return reasons or ["quality_improving"]

    def _evidence(self, state: LoopState) -> dict[str, Any]:
        """The numbers behind the decision - everything needed to audit it later."""
        policies = self._policies
        report = state.report
        evidence: dict[str, Any] = {
            "iteration": state.iteration,
            "max_iterations": policies.max_iterations,
            "q_history": list(state.q_history),
            "q_latest": (state.q_history[-1] if state.q_history else None),
            "q_min": policies.q_min,
            "run_tokens": state.run_tokens,
            "run_token_cap": policies.run_token_cap,
            "iteration_tokens": (state.iteration_tokens[-1] if state.iteration_tokens else 0),
            "iteration_token_cap": policies.iteration_token_cap,
            "error_streak": state.error_streak,
            "error_streak_limit": policies.error_streak,
            "issues_opened": state.issues_opened,
            "issues_closed": state.issues_closed,
            "issue_churn_ratio": round(state.issue_churn_ratio, 4),
            "failed_gates": (report.failed_gates if report is not None else []),
            "high_severity_issues": (
                len(report.high_severity_issues()) if report is not None else 0
            ),
            "verdict_meets_minimum": (
                state.verdict.meets_minimum if state.verdict is not None else None
            ),
            "verdict_recommendation": (
                state.verdict.recommendation.value if state.verdict is not None else None
            ),
            "recurred_sections": list(state.recurred_sections),
            "erosion_detectors": detect_erosion(state, policies),
        }
        return evidence

    @staticmethod
    def _decision(
        state: LoopState, action: LoopAction, reasons: list[str], evidence: dict[str, Any]
    ) -> LoopDecision:
        return LoopDecision(iter=state.iteration, action=action, reasons=reasons, evidence=evidence)
