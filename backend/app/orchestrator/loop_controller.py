"""The loop controller: a pure function from run history to a ``LoopDecision``.

Step 9a. This module is the deterministic heart of ScholarLoop's termination
guarantee. It contains no I/O, no provider, no clock and no randomness: the same
:class:`LoopHistory` and the same :class:`~app.core.policies.Policies` always
produce the same :class:`~app.contracts.loop.LoopDecision`, which is what makes
the property test ("for *any* sequence of reports the loop stops within
``max_iterations``") meaningful.

Detectors, in strict precedence order (first hit wins):

1. ``user_stop`` - the user asked to stop. Always wins. ``STOP_USER``.
2. ``caps`` - iteration / run-token / wall-clock / account-budget ceilings.
   ``STOP_BUDGET``. The iteration cap is what makes termination unconditional:
   it fires on iteration count alone, whatever the reports say.
3. ``error_streak`` - ``policies.error_streak`` consecutive failed ledger
   entries. ``STOP_ERRORS``.
4. ``needs_user_input`` - an agent returned ``needs_user_input`` in the latest
   iteration (HARD RULE 1). ``ASK_USER``; the loop cannot invent the missing
   source, datum or quote, so it must not continue past this point.
5. ``integrity_drift`` (soft, two strikes) - the verified-citation ratio fell
   versus the previous report, or the count of unverified citations grew.
6. ``regression`` / ``oscillation`` / ``plateau`` (soft, two strikes).
7. ``completion`` - every hard gate passes, ``Q >= q_min`` and no
   high-severity issue is open. Emits ``OFFER_FINISH``; when no Planner
   verdict exists for the iteration yet, the evidence carries
   ``needs_planner_evaluation=True`` so the orchestrator asks the Planner
   before offering anything to the user (Step 9, item 5). A verdict that says
   "continue" vetoes completion for that iteration.
8. ``diminishing_returns`` / ``issue_churn`` (soft, two strikes).
9. Otherwise ``CONTINUE``.

Two-strike rule for soft signals: strike 1 returns ``CONTINUE`` with
``evidence["strategy_change"]=True`` (the orchestrator replans with
``strategy_change=True``); strike 2 for the same detector returns
``STOP_EROSION``. Strikes live in :attr:`LoopHistory.strikes` and are
advanced by :func:`next_strikes`, which the orchestrator persists after
every decision. Strikes accumulate per detector (alternating detectors
cannot dodge them) and are forgiven when an iteration reaches a strictly
new best Q.

Every decision carries evidence: the Q series, tokens spent, the best Q seen,
the latest failed gates, the active strikes and the winning detector's own
numbers. Reasons are stable ``snake_case`` codes only (the ``LoopDecision``
contract enforces this).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

from pydantic import Field

from app.contracts.common import Contract
from app.contracts.enums import ChangeStatus, LoopAction, Recommendation
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.loop import LoopDecision
from app.contracts.quality import QualityReport
from app.contracts.verdict import Verdict
from app.core.policies import DEFAULT_POLICIES, Policies

__all__ = [
    "HARD_DETECTORS",
    "SOFT_DETECTORS",
    "DetectorHit",
    "IterationRecord",
    "LoopController",
    "LoopHistory",
    "detect_caps",
    "detect_completion",
    "detect_diminishing_returns",
    "detect_error_streak",
    "detect_integrity_drift",
    "detect_issue_churn",
    "detect_needs_user_input",
    "detect_oscillation",
    "detect_plateau",
    "detect_regression",
    "detect_user_stop",
    "evaluate",
    "next_strikes",
]

#: Detectors whose hit is a *soft* signal: strike 1 replans, strike 2 stops.
SOFT_DETECTORS: Final[frozenset[str]] = frozenset(
    {
        "integrity_drift",
        "regression",
        "oscillation",
        "plateau",
        "diminishing_returns",
        "issue_churn",
    }
)

#: Detectors that stop (or pause for the user) on first hit.
HARD_DETECTORS: Final[frozenset[str]] = frozenset(
    {"user_stop", "caps", "error_streak", "needs_user_input", "completion"}
)

#: Strikes required before a soft signal becomes ``STOP_EROSION``.
STRIKES_TO_STOP: Final[int] = 2


def _zero_usage() -> TokenUsage:
    return TokenUsage(in_=0, out=0, cached=0)


class IterationRecord(Contract):
    """Everything the controller may know about one finished iteration.

    ``report`` is ``None`` when the iteration produced no validator report
    (the validator task failed, or the plan contained none). The controller
    never invents a Q for such an iteration (HARD RULE 1); it can still stop
    on caps and error streaks.
    """

    iter: int = Field(ge=0, le=10_000)
    report: QualityReport | None = None
    entries: list[ChangeEntry] = Field(default_factory=list)
    tokens: TokenUsage = Field(default_factory=_zero_usage)
    wall_seconds: float = Field(default=0.0, ge=0.0)
    unverified_citations: int = Field(
        default=0,
        ge=0,
        description="Citation sites with no verified SourceCard behind them, at iteration end.",
    )


class LoopHistory(Contract):
    """The complete, serialisable input to :func:`evaluate`.

    Caps that ``Policies`` does not own (wall clock, a per-account monthly
    budget, user-granted extensions from a checkpoint) travel on the history
    so the controller stays a pure function of ``(history, policies)``.
    """

    records: list[IterationRecord] = Field(default_factory=list)
    user_stop_requested: bool = False
    tokens_spent: int | None = Field(
        default=None,
        ge=0,
        description="Run tokens spent (agents + planner). None = sum the records.",
    )
    wall_seconds: float | None = Field(
        default=None, ge=0.0, description="Elapsed run wall time. None = sum the records."
    )
    verdict: Verdict | None = Field(
        default=None, description="Planner verdict for the latest iteration, when evaluated."
    )
    strikes: dict[str, int] = Field(
        default_factory=dict, description="Consecutive soft-detector strikes, by detector name."
    )
    max_iterations: int | None = Field(
        default=None, ge=1, description="Override for the policy cap."
    )
    token_cap: int | None = Field(default=None, ge=1, description="Override for run_token_cap.")
    max_wall_seconds: float | None = Field(default=None, gt=0.0)
    account_budget_tokens: int | None = Field(default=None, ge=1)
    account_tokens_spent: int | None = Field(default=None, ge=0)

    # -- derived views ------------------------------------------------------
    @property
    def current_iter(self) -> int:
        """Iteration number of the latest record (0 before the first)."""
        return self.records[-1].iter if self.records else 0

    @property
    def reports(self) -> list[QualityReport]:
        """Validator reports in iteration order (records without one skipped)."""
        return [record.report for record in self.records if record.report is not None]

    @property
    def q_series(self) -> list[int]:
        """Q per reported iteration, in order."""
        return [report.Q for report in self.reports]

    @property
    def latest_report(self) -> QualityReport | None:
        """Most recent validator report, or ``None``."""
        reports = self.reports
        return reports[-1] if reports else None

    @property
    def entries(self) -> list[ChangeEntry]:
        """Every ledger entry, oldest first."""
        return [entry for record in self.records for entry in record.entries]

    def resolved_tokens_spent(self) -> int:
        """Run tokens: the explicit figure when given, else the records' sum."""
        if self.tokens_spent is not None:
            return self.tokens_spent
        return sum(record.tokens.total for record in self.records)

    def resolved_wall_seconds(self) -> float:
        """Elapsed wall time: the explicit figure when given, else the sum."""
        if self.wall_seconds is not None:
            return self.wall_seconds
        return sum(record.wall_seconds for record in self.records)

    def best_q(self) -> int | None:
        """Highest Q ever reported, or ``None`` when nothing was measured."""
        series = self.q_series
        return max(series) if series else None

    def best_iter(self) -> int | None:
        """Iteration that produced :meth:`best_q` (latest one on ties)."""
        best = self.best_q()
        if best is None:
            return None
        found: int | None = None
        for record in self.records:
            if record.report is not None and best == record.report.Q:
                found = record.iter
        return found


@dataclass(frozen=True)
class DetectorHit:
    """One detector fired. ``name`` is the stable detector code."""

    name: str
    reason: str
    evidence: dict[str, Any]

    @property
    def is_soft(self) -> bool:
        """True when the two-strike rule applies to this detector."""
        return self.name in SOFT_DETECTORS


# --------------------------------------------------------------------------- detectors
def detect_user_stop(history: LoopHistory) -> DetectorHit | None:
    """The user pressed stop. Highest precedence, no thresholds."""
    if not history.user_stop_requested:
        return None
    return DetectorHit("user_stop", "user_stop_requested", {})


def detect_caps(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Any hard ceiling reached: iterations, tokens, wall clock, account budget."""
    max_iterations = history.max_iterations or policies.max_iterations
    if history.records and history.current_iter >= max_iterations:
        return DetectorHit(
            "caps",
            "max_iterations_reached",
            {"iter": history.current_iter, "max_iterations": max_iterations},
        )
    token_cap = history.token_cap or policies.run_token_cap
    spent = history.resolved_tokens_spent()
    if spent >= token_cap:
        return DetectorHit(
            "caps",
            "run_token_cap_reached",
            {"tokens_spent": spent, "token_cap": token_cap},
        )
    if history.max_wall_seconds is not None:
        wall = history.resolved_wall_seconds()
        if wall >= history.max_wall_seconds:
            return DetectorHit(
                "caps",
                "wall_clock_cap_reached",
                {"wall_seconds": wall, "max_wall_seconds": history.max_wall_seconds},
            )
    if (
        history.account_budget_tokens is not None
        and history.account_tokens_spent is not None
        and history.account_tokens_spent >= history.account_budget_tokens
    ):
        return DetectorHit(
            "caps",
            "account_budget_exhausted",
            {
                "account_tokens_spent": history.account_tokens_spent,
                "account_budget_tokens": history.account_budget_tokens,
            },
        )
    return None


def detect_error_streak(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """``error_streak`` consecutive ``failed`` entries at the ledger tail."""
    streak = 0
    for entry in reversed(history.entries):
        if entry.status is ChangeStatus.FAILED:
            streak += 1
        else:
            break
    if streak < policies.error_streak:
        return None
    return DetectorHit(
        "error_streak",
        "error_streak_reached",
        {"failed_streak": streak, "error_streak_limit": policies.error_streak},
    )


def detect_needs_user_input(history: LoopHistory) -> DetectorHit | None:
    """Latest iteration has a ``needs_user_input`` entry (HARD RULE 1)."""
    if not history.records:
        return None
    blocked = [
        entry.id
        for entry in history.records[-1].entries
        if entry.status is ChangeStatus.NEEDS_USER_INPUT
    ]
    if not blocked:
        return None
    return DetectorHit(
        "needs_user_input",
        "needs_user_input",
        {"blocked_entry_ids": blocked},
    )


def detect_integrity_drift(history: LoopHistory) -> DetectorHit | None:
    """Verified-citation ratio fell, or unverified citations appeared/grew.

    A falling ratio means the article is accumulating claims whose sources are
    not verified - the failure mode HARD RULE 1 exists to prevent. The count
    signal catches the same drift before any ratio moves (the first
    unverified citation after a clean iteration).
    """
    reports = history.reports
    if len(reports) >= 2:
        previous, latest = reports[-2], reports[-1]
        if latest.verified_citation_ratio < previous.verified_citation_ratio - 1e-9:
            return DetectorHit(
                "integrity_drift",
                "verified_citation_ratio_fell",
                {
                    "previous_ratio": previous.verified_citation_ratio,
                    "latest_ratio": latest.verified_citation_ratio,
                },
            )
    counts = [record.unverified_citations for record in history.records]
    if len(counts) >= 2 and counts[-1] > counts[-2]:
        return DetectorHit(
            "integrity_drift",
            "unverified_references_appeared",
            {"previous_unverified": counts[-2], "latest_unverified": counts[-1]},
        )
    return None


def detect_regression(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Q fell by ``delta`` (inside the window or from the best), or a gate relapsed.

    A gate relapse - a hard gate that passed in some earlier report and fails
    in the latest - is the structural form of regression: work that was true
    stopped being true.
    """
    reports = history.reports
    if len(reports) < 2:
        return None
    latest = reports[-1]
    best = max(report.Q for report in reports)
    window = reports[-1 - policies.regression.window : -1] or reports[:-1]
    window_reference = max(report.Q for report in window)
    window_drop = window_reference - latest.Q
    best_drop = best - latest.Q
    if window_drop >= policies.regression.delta or best_drop >= policies.regression.delta:
        return DetectorHit(
            "regression",
            "q_regressed",
            {
                "latest_q": latest.Q,
                "window_reference_q": window_reference,
                "window_drop": window_drop,
                "best_q": best,
                "best_drop": best_drop,
                "regression_delta": policies.regression.delta,
            },
        )
    passed_before = {
        name for report in reports[:-1] for name, passed in report.gates.items() if passed
    }
    relapsed = [name for name in latest.failed_gates if name in passed_before]
    if relapsed:
        return DetectorHit(
            "regression",
            "gate_regressed",
            {"regressed_gates": relapsed, "latest_q": latest.Q},
        )
    return None


def detect_oscillation(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Q alternates up/down (strictly, no flat steps) over the window."""
    window = policies.oscillation.window
    series = history.q_series
    if len(series) < window:
        return None
    tail = series[-window:]
    diffs = [later - earlier for earlier, later in pairwise(tail)]
    if any(diff == 0 for diff in diffs):
        return None
    signs = [1 if diff > 0 else -1 for diff in diffs]
    alternates = all(a == -b for a, b in pairwise(signs))
    if not alternates:
        return None
    return DetectorHit(
        "oscillation",
        "q_oscillating",
        {"window_q": tail, "diffs": diffs, "window": window},
    )


def detect_plateau(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Gain over the last ``window`` reports is below ``min_gain`` Q points."""
    window = policies.plateau.window
    series = history.q_series
    if len(series) < window:
        return None
    tail = series[-window:]
    gain = tail[-1] - tail[0]
    if gain >= policies.plateau.min_gain:
        return None
    return DetectorHit(
        "plateau",
        "plateau_detected",
        {"window_q": tail, "gain": gain, "min_gain": policies.plateau.min_gain},
    )


def detect_completion(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """All gates pass, ``Q >= q_min``, no high-severity issue is open.

    When a Planner verdict is present it must agree (``meets_minimum`` and
    ``recommendation=finish``); a "continue" verdict vetoes completion. When
    no verdict is present the hit is still returned - flagged with
    ``needs_planner_evaluation`` - because the orchestrator must call
    ``Planner.evaluate`` before the offer reaches the user.
    """
    latest = history.latest_report
    if latest is None or not latest.all_gates_pass or policies.q_min > latest.Q:
        return None
    if latest.high_severity_issues():
        return None
    evidence: dict[str, Any] = {
        "latest_q": latest.Q,
        "q_min": policies.q_min,
        "failed_gates": [],
    }
    verdict = history.verdict
    if verdict is None:
        evidence["needs_planner_evaluation"] = True
        return DetectorHit("completion", "completion_candidate", evidence)
    if verdict.meets_minimum and verdict.recommendation is Recommendation.FINISH:
        evidence["needs_planner_evaluation"] = False
        evidence["verdict_recommendation"] = verdict.recommendation.value
        return DetectorHit("completion", "completion_candidate", evidence)
    return None


def detect_diminishing_returns(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Gain per 10k tokens below the floor for ``window`` consecutive intervals.

    Intervals with zero measured tokens are skipped: a run that spent nothing
    (scripted simulation) cannot be judged on return-per-token.
    """
    window = policies.diminishing.window
    reported = [record for record in history.records if record.report is not None]
    if len(reported) < window + 1:
        return None
    rates: list[float] = []
    for previous, current in pairwise(reported):
        tokens = current.tokens.total
        if tokens <= 0:
            continue
        assert previous.report is not None and current.report is not None
        gain = current.report.Q - previous.report.Q
        rates.append(gain / (tokens / 10_000))
    if len(rates) < window:
        return None
    tail = rates[-window:]
    if any(rate >= policies.diminishing.min_gain_per_10k for rate in tail):
        return None
    return DetectorHit(
        "diminishing_returns",
        "diminishing_returns",
        {
            "gain_per_10k": [round(rate, 4) for rate in tail],
            "min_gain_per_10k": policies.diminishing.min_gain_per_10k,
            "window": window,
        },
    )


def detect_issue_churn(history: LoopHistory, policies: Policies) -> DetectorHit | None:
    """Issues keep opening without closing (churn ratio over the policy).

    Uses the ledger-wide definition from Step 4 (``IssueSummary.churn_ratio``
    = still-open / total-opened). Requires at least two iterations and three
    opened issues so a first iteration that merely *found* problems is not
    misread as churn.
    """
    if len(history.records) < 2:
        return None
    open_ids: list[str] = []
    total_opened = 0
    total_closed = 0
    for entry in history.entries:
        for issue_id in entry.issues_opened:
            total_opened += 1
            if issue_id not in open_ids:
                open_ids.append(issue_id)
        for issue_id in entry.issues_closed:
            total_closed += 1
            if issue_id in open_ids:
                open_ids.remove(issue_id)
    if total_opened < 3 or total_opened <= total_closed:
        return None
    ratio = len(open_ids) / total_opened
    if ratio <= policies.issue_churn:
        return None
    return DetectorHit(
        "issue_churn",
        "issue_churn_detected",
        {
            "churn_ratio": round(ratio, 4),
            "issue_churn_limit": policies.issue_churn,
            "total_opened": total_opened,
            "total_closed": total_closed,
            "still_open": len(open_ids),
        },
    )


# --------------------------------------------------------------------------- evaluation
def _base_evidence(history: LoopHistory, policies: Policies) -> dict[str, Any]:
    latest = history.latest_report
    return {
        "iter": history.current_iter,
        "q_series": history.q_series,
        "best_q": history.best_q(),
        "best_iter": history.best_iter(),
        "tokens_spent": history.resolved_tokens_spent(),
        "token_cap": history.token_cap or policies.run_token_cap,
        "max_iterations": history.max_iterations or policies.max_iterations,
        "failed_gates": latest.failed_gates if latest is not None else [],
        "strikes": dict(history.strikes),
    }


def evaluate(history: LoopHistory, policies: Policies = DEFAULT_POLICIES) -> LoopDecision:
    """Decide what the loop does next. Pure: no I/O, no clocks, no mutation."""
    evidence = _base_evidence(history, policies)
    iteration = history.current_iter

    def decision(action: LoopAction, reasons: list[str], extra: dict[str, Any]) -> LoopDecision:
        return LoopDecision(
            iter=iteration, action=action, reasons=reasons, evidence={**evidence, **extra}
        )

    # 1-2. User, then caps: both unconditional.
    if (hit := detect_user_stop(history)) is not None:
        return decision(LoopAction.STOP_USER, [hit.reason], {"detector": hit.name})
    if (hit := detect_caps(history, policies)) is not None:
        return decision(
            LoopAction.STOP_BUDGET, [hit.reason], {"detector": hit.name, **hit.evidence}
        )
    # 3-4. Errors and the HARD RULE 1 pause.
    if (hit := detect_error_streak(history, policies)) is not None:
        return decision(
            LoopAction.STOP_ERRORS, [hit.reason], {"detector": hit.name, **hit.evidence}
        )
    if (hit := detect_needs_user_input(history)) is not None:
        return decision(LoopAction.ASK_USER, [hit.reason], {"detector": hit.name, **hit.evidence})

    # 5-6, 8. Soft signals, two strikes, in precedence order. Completion (7)
    # sits between the plateau group and the diminishing group.
    def soft(hit: DetectorHit) -> LoopDecision:
        strikes = history.strikes.get(hit.name, 0) + 1
        extra = {"detector": hit.name, "strike": strikes, **hit.evidence}
        if strikes >= STRIKES_TO_STOP:
            return decision(LoopAction.STOP_EROSION, [hit.reason, "second_strike"], extra)
        return decision(
            LoopAction.CONTINUE,
            [hit.reason, "strategy_change"],
            {**extra, "strategy_change": True},
        )

    erosion_detectors: tuple[Callable[[], DetectorHit | None], ...] = (
        lambda: detect_integrity_drift(history),
        lambda: detect_regression(history, policies),
        lambda: detect_oscillation(history, policies),
        lambda: detect_plateau(history, policies),
    )
    for detector in erosion_detectors:
        if (hit := detector()) is not None:
            return soft(hit)

    # 7. Completion candidate.
    if (hit := detect_completion(history, policies)) is not None:
        return decision(
            LoopAction.OFFER_FINISH, [hit.reason], {"detector": hit.name, **hit.evidence}
        )

    late_detectors: tuple[Callable[[], DetectorHit | None], ...] = (
        lambda: detect_diminishing_returns(history, policies),
        lambda: detect_issue_churn(history, policies),
    )
    for detector in late_detectors:
        if (hit := detector()) is not None:
            return soft(hit)

    # 9. No detector fired: why are we continuing? Say so explicitly.
    latest = history.latest_report
    if latest is None:
        reasons = ["no_quality_report_yet"]
    elif policies.q_min > latest.Q:
        reasons = ["q_below_minimum"]
        if latest.failed_gates:
            reasons.append("gates_failing")
    elif latest.failed_gates:
        reasons = ["gates_failing"]
    elif history.verdict is not None and not history.verdict.meets_minimum:
        reasons = ["verdict_below_minimum"]
    else:
        reasons = ["quality_improving"]
    return decision(LoopAction.CONTINUE, reasons, {"detector": None})


def next_strikes(history: LoopHistory, decision: LoopDecision) -> dict[str, int]:
    """Advance the strike table after ``decision``. Persist the result.

    Strikes accumulate per detector and survive other detectors firing in
    between - otherwise an oscillating run could alternate regression and
    oscillation hits forever, never taking a second strike from either. The
    one absolution is *measured progress*: a clean ``CONTINUE`` whose latest
    Q is a strictly new best clears the table, so an early plateau does not
    hang over a run that subsequently starts improving.
    """
    detector = decision.evidence.get("detector")
    if (
        decision.action is LoopAction.CONTINUE
        and decision.evidence.get("strategy_change") is True
        and isinstance(detector, str)
        and detector in SOFT_DETECTORS
    ):
        return {**history.strikes, detector: history.strikes.get(detector, 0) + 1}
    if decision.action in {LoopAction.CONTINUE, LoopAction.OFFER_FINISH}:
        series = history.q_series
        if series and (len(series) == 1 or series[-1] > max(series[:-1])):
            return {}
        return dict(history.strikes)
    return {}


class LoopController:
    """Thin object wrapper over :func:`evaluate` with bound policies."""

    def __init__(self, policies: Policies = DEFAULT_POLICIES) -> None:
        self._policies = policies

    @property
    def policies(self) -> Policies:
        """The policy set this controller enforces."""
        return self._policies

    def evaluate(self, history: LoopHistory) -> LoopDecision:
        """Decide the next loop action for ``history`` (pure)."""
        return evaluate(history, self._policies)

    def next_strikes(self, history: LoopHistory, decision: LoopDecision) -> dict[str, int]:
        """Strike table to persist after ``decision``."""
        return next_strikes(history, decision)
