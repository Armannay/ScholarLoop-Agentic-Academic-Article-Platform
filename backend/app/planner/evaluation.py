"""Merge the Planner's judgement with the deterministic gates into one ``Verdict``.

The split of authority is the whole design:

* the **probes** decide the 8 hard gates and are facts;
* the **Planner** scores the rubric's soft criteria and explains what is missing;
* the **merge** below is code, and it can only ever make the verdict *stricter*.

So ``meets_minimum`` is ``planner.meets_minimum AND every gate passes AND Q >= q_min``.
A planner that says "done" while a gate fails is overruled, and the failed gate is named
in ``missing`` with the probe's own detail - which is what makes the verdict concrete
enough for the next plan to act on.

``Q`` is recomputed from the rubric weights rather than taken from the model: a weighted
average nobody can audit is not a quality score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from app.contracts.enums import Recommendation
from app.contracts.ledger import TokenUsage
from app.contracts.verdict import Verdict
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.planner.drafts import EvaluationDraft
from app.planner.probes import ProbeReport

__all__ = [
    "Evaluation",
    "criteria_names",
    "evaluation_problems",
    "merge_verdict",
    "weighted_q",
]

WEIGHT_TOTAL: Final[float] = 100.0


@dataclass(frozen=True)
class Evaluation:
    """One merged evaluation: the verdict plus the numbers it was derived from."""

    verdict: Verdict
    scores: dict[str, int]
    q: int
    gates: dict[str, bool]
    failed_gates: tuple[str, ...]
    planner_meets_minimum: bool
    attempts: int
    tokens: TokenUsage
    section_scores: dict[str, int] = field(default_factory=dict)
    deep_checked: tuple[str, ...] = ()

    @property
    def meets_minimum(self) -> bool:
        """The merged answer, i.e. what the loop controller may act on."""
        return self.verdict.meets_minimum


def criteria_names(rubric: Rubric) -> tuple[str, ...]:
    """Soft criteria the evaluation must score, in rubric order.

    Read from ``Rubric.weights`` so the criteria, their order and their weights all come
    from the one prerequisite accessor - the planner owns no copy of the rubric.
    """
    return tuple(rubric.weights())


def evaluation_problems(draft: EvaluationDraft, *, rubric: Rubric) -> list[str]:
    """Deterministic problems with an evaluation draft, in stable order.

    A draft is rejected when it skips a criterion, invents one, or contradicts the
    ``Verdict`` contract. A draft that merely *claims* completion while a gate fails is
    not rejected: the merge overrules it, and spending a retry on it would only make the
    model repeat the claim.
    """
    problems: list[str] = []
    expected = criteria_names(rubric)
    missing = [name for name in expected if name not in draft.scores]
    if missing:
        problems.append(
            f"scores: missing {missing}; every rubric criterion must be scored "
            f"({', '.join(expected)})"
        )
    unknown = sorted(set(draft.scores) - set(expected))
    if unknown:
        problems.append(f"scores: unknown criteria {unknown}; the rubric defines {list(expected)}")
    if (
        draft.meets_minimum
        and draft.recommendation is Recommendation.CONTINUE
        and not draft.missing
    ):
        problems.append(
            "missing: recommendation=continue with meets_minimum=true needs at least one "
            "concrete requirement; otherwise the loop would iterate without a target"
        )
    if not draft.meets_minimum and draft.recommendation is Recommendation.FINISH:
        problems.append(
            "recommendation: finish contradicts meets_minimum=false; the controller cannot "
            "accept a sub-standard article"
        )
    return problems


def weighted_q(scores: dict[str, int], rubric: Rubric) -> int:
    """Rubric-weighted Q, rounded half-up to the contract's integer scale.

    Raises:
        KeyError: a rubric criterion is missing from ``scores`` - callers validate the
            draft first, so a missing key here is a programming error, not model output.
    """
    weights = rubric.weights()
    for name in weights:
        if name not in scores:
            raise KeyError(name)
    total = sum(scores[name] * weight for name, weight in weights.items())
    return max(0, min(100, int(total / WEIGHT_TOTAL + 0.5)))


def merge_verdict(
    *,
    draft: EvaluationDraft,
    probes: ProbeReport,
    policies: Policies,
    rubric: Rubric,
    report_high_issues: int = 0,
) -> Verdict:
    """Combine the draft with the gates into a ``Verdict`` that only gets stricter.

    Args:
        draft: the Planner's scores and explanation.
        probes: the deterministic gate results and the view they measured.
        policies: supplies ``q_min``.
        rubric: supplies the criteria weights.
        report_high_issues: high-severity issues still open in the validator's report;
            any such issue blocks ``meets_minimum``.
    """
    gates = probes.gate_map
    failed = [name for name in gates if not gates[name]]
    q = weighted_q(draft.scores, rubric)
    q_ok = q >= policies.q_min
    meets_minimum = bool(draft.meets_minimum and not failed and q_ok and report_high_issues == 0)

    missing: list[str] = []
    for name in failed:
        gate = probes.gate(name)
        missing.append(f"hard gate '{name}' fails: {gate.detail}")
    if not q_ok:
        missing.append(f"Q is {q}, below the q_min of {policies.q_min}")
    if report_high_issues:
        missing.append(
            f"{report_high_issues} high-severity issue(s) are still open in the validator report"
        )
    missing.extend(draft.missing)
    deduped_missing = list(dict.fromkeys(missing))

    recommendation = Recommendation.CONTINUE
    if meets_minimum and (draft.recommendation is Recommendation.FINISH or not deduped_missing):
        # When nothing is missing the only coherent recommendation is to finish: a
        # "continue" without a target would iterate for free.
        recommendation = Recommendation.FINISH

    return Verdict(
        meets_minimum=meets_minimum,
        missing=deduped_missing,
        risks=list(dict.fromkeys(draft.risks)),
        recommendation=recommendation,
    )
