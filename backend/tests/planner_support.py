"""Builders shared by the Step 8 test-suite.

Everything here is synthetic and deterministic: fixed ULIDs, a ``MockProvider``, and
documents assembled from the real ``config/`` and ``rules/`` files so the tests exercise
the same policies, rubric and prompts that production loads.

No live provider, no key-shaped strings, no wall clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.agents.binding import MappingBindingResolver
from app.agents.governor import CallBudgetGovernor
from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.enums import (
    ArticleType,
    BindingRole,
    CitationStyle,
    IssueSeverity,
    SectionStatus,
    VerificationStatus,
)
from app.contracts.llm import LLMRequest, LLMResponse
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.contracts.section import CitationRef, SectionState
from app.contracts.source import SourceCard
from app.core.hashing import content_hash
from app.core.ids import UlidFactory
from app.core.policies import Policies
from app.core.rubric import Rubric
from app.planner import (
    ArticleView,
    PlanBudget,
    PlannerContexts,
    PlannerService,
    build_planner_contexts,
)
from app.providers.mock import MockProvider, Script
from app.skills.loader import SkillLoader
from tests.factories import FIXED_TS_MS, sample_ulid

__all__ = [
    "REPO_ROOT",
    "SOURCE_A",
    "SOURCE_B",
    "SOURCE_C",
    "Harness",
    "RecordingGovernor",
    "SpyProvider",
    "article_view",
    "brief_for",
    "full_view",
    "make_binding",
    "make_contexts",
    "make_service",
    "plan_payload",
    "prose",
    "quality_report",
    "section",
    "source",
    "ulid_factory",
]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Stable source-card ids used by the probe fixtures (real ULIDs: the contract checks).
SOURCE_A: Final[str] = sample_ulid(30)
SOURCE_B: Final[str] = sample_ulid(31)
SOURCE_C: Final[str] = sample_ulid(32)


def ulid_factory() -> UlidFactory:
    """Deterministic ULID source, identical on every run."""
    return UlidFactory(timestamp_ms=FIXED_TS_MS)


def make_binding(
    *,
    model: str = "claude-planner-test",
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 4_096,
    enabled: bool = True,
) -> AgentBinding:
    """A planner binding over one synthetic account."""
    account_ids = [sample_ulid(40)]
    return AgentBinding(
        role=BindingRole.PLANNER,
        account_ids=account_ids,
        strategy="dedicated",
        model=model,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        enabled=enabled,
    )


def make_contexts(
    rubric: Rubric,
    *,
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 4_096,
) -> PlannerContexts:
    """The two planner contexts, built from the repository's rules and skills."""
    return build_planner_contexts(
        rules_dir=REPO_ROOT / "rules",
        rubric=rubric,
        skills=SkillLoader(REPO_ROOT / "skills"),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )


class SpyProvider:
    """Wraps a provider and records every request, so tests can inspect prompts."""

    name = "spy"

    def __init__(self, inner: MockProvider) -> None:
        self._inner = inner
        self.requests: list[LLMRequest] = []

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
    ) -> LLMResponse:
        self.requests.append(request)
        return await self._inner.generate(request, model=model, role=role)

    async def count_tokens(self, request: LLMRequest) -> int | None:
        return await self._inner.count_tokens(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class RecordingGovernor:
    """Records prechecks so a test can prove the governor was actually consulted."""

    def __init__(self) -> None:
        self.calls: list[tuple[BindingRole, str]] = []

    def precheck(self, request: LLMRequest, *, role: BindingRole, task: Any) -> None:
        """Record the precheck; the request itself is not needed for the assertions."""
        del request
        self.calls.append((role, task.id))


@dataclass
class Harness:
    """A service plus the doubles it was built with."""

    service: PlannerService
    provider: MockProvider
    spy: SpyProvider
    governor: Any
    binding: AgentBinding | None
    ids: UlidFactory


def make_service(
    *,
    rubric: Rubric,
    policies: Policies,
    brief: Brief | None = None,
    script: Script | None = None,
    binding: AgentBinding | None = None,
    contexts: PlannerContexts | None = None,
    governor: Any = None,
    max_retries: int = 2,
) -> Harness:
    """Build a ``PlannerService`` over a ``MockProvider`` with a recording spy."""
    mock = MockProvider(script if script is not None else Script())
    spy = SpyProvider(mock)
    ids = ulid_factory()
    resolved_binding = binding if binding is not None else make_binding()
    resolved_governor = governor if governor is not None else CallBudgetGovernor(policies)
    resolver = MappingBindingResolver({BindingRole.PLANNER: resolved_binding})
    service = PlannerService(
        provider=spy,
        bindings=resolver if resolved_binding.is_active else MappingBindingResolver({}),
        governor=resolved_governor,
        contexts=contexts if contexts is not None else make_contexts(rubric),
        rubric=rubric,
        policies=policies,
        brief=brief,
        id_factory=ids,
        max_retries=max_retries,
    )
    return Harness(
        service=service,
        provider=mock,
        spy=spy,
        governor=resolved_governor,
        binding=resolved_binding,
        ids=ids,
    )


def brief_for(article_type: ArticleType) -> Brief:
    """A brief per article type, with a realistic target length."""
    targets = {
        ArticleType.RESEARCH: 4_000,
        ArticleType.REVIEW: 5_000,
        ArticleType.POSITION: 3_000,
        ArticleType.CASE_STUDY: 3_500,
    }
    styles = {
        ArticleType.RESEARCH: CitationStyle.APA7,
        ArticleType.REVIEW: CitationStyle.HARVARD,
        ArticleType.POSITION: CitationStyle.APA7,
        ArticleType.CASE_STUDY: CitationStyle.IEEE,
    }
    return Brief(
        topic=f"A {article_type.value} article on token-frugal agentic writing",
        article_type=article_type,
        audience="graduate students and applied NLP practitioners",
        language="en",
        target_words=targets[article_type],
        citation_style=styles[article_type],
        must_include=[],
        must_avoid=[],
        provided_data=[],
    )


def plan_payload(
    *,
    sections: Sequence[str],
    goal: str = "A verified article at Q >= 70.",
    role_override: str | None = None,
    token_budget: int = 4_000,
    depends_on: Sequence[str] = (),
) -> dict[str, Any]:
    """A well-formed plan draft covering the four required roles.

    Sections are handed to the architect first, then to the researcher, writer and
    validator, which is exactly the create-then-use order the validator checks.
    """
    first = sections[0]
    return {
        "goal": goal,
        "tasks": [
            {
                "key": "T1",
                "title": "Outline the article structure",
                "role": role_override or "architect",
                "section_ids": list(sections),
                "instructions": "Produce a section outline with word targets per section.",
                "acceptance": ["Outline lists every required section"],
                "token_budget": 1_000,
                "depends_on": [],
            },
            {
                "key": "T2",
                "title": "Collect and card primary sources",
                "role": "researcher",
                "section_ids": [first],
                "instructions": "Find six primary sources and record locatable cards.",
                "acceptance": ["Every card has a DOI or URL"],
                "token_budget": token_budget,
                "depends_on": ["T1"],
            },
            {
                "key": "T3",
                "title": "Draft the opening section",
                "role": "writer",
                "section_ids": [first],
                "instructions": "Open with the gap, then state the contribution.",
                "acceptance": ["No unsupported claims in the draft"],
                "token_budget": token_budget,
                "depends_on": ["T2"],
            },
            {
                "key": "T4",
                "title": "Validate the draft against the rubric",
                "role": "validator",
                "section_ids": [first],
                "instructions": "Compute the eight gates and score the soft criteria.",
                "acceptance": ["All eight hard gates reported"],
                "token_budget": 2_000,
                "depends_on": list(depends_on) or ["T3"],
            },
        ],
    }


def prose(sentences: int = 4) -> str:
    """Deterministic prose with ``sentences`` well-formed sentences."""
    return " ".join(
        f"Sentence {index} about the article state." for index in range(1, sentences + 1)
    )


def section(
    section_id: str,
    *,
    words: int,
    order: int,
    title: str | None = None,
    citations: Sequence[CitationRef] = (),
    text: str | None = None,
    status: SectionStatus = SectionStatus.DRAFT,
) -> SectionState:
    """A section state with a matching hash and the given metadata."""
    body = text if text is not None else prose()
    return SectionState(
        id=section_id,
        title=title or section_id.replace("-", " ").title(),
        order=order,
        version=1,
        hash=content_hash(body),
        words=words,
        citations=list(citations),
        status=status,
        open_issues=[],
    )


def source(
    source_id: str,
    *,
    verified: bool = True,
    year: int = 2024,
    suffix: str = "a",
) -> SourceCard:
    """A locatable source card, verified by default."""
    return SourceCard(
        id=source_id,
        title=f"Budgeted multi-agent writing {suffix}",
        authors=["Nguyen, L."],
        year=year,
        venue="Journal of Applied NLP Systems",
        doi=f"10.1234/jans.2024.{suffix}",
        url=None,
        snippet="The authors report that a bounded controller keeps prompt growth linear.",
        verification=VerificationStatus.VERIFIED if verified else VerificationStatus.UNKNOWN,
    )


ARTICLE_LAYOUT: Final[tuple[tuple[str, int], ...]] = (
    ("abstract", 200),
    ("introduction", 600),
    ("related-work", 700),
    ("methodology", 800),
    ("results", 600),
    ("discussion", 700),
    ("limitations", 150),
    ("conclusion", 200),
    ("references", 0),
)


def article_view(
    brief: Brief,
    *,
    layout: Sequence[tuple[str, int]] = ARTICLE_LAYOUT,
    citation_keys: Sequence[tuple[str, str, str | None]] = (
        ("introduction", "(Smith, 2020)", SOURCE_A),
        ("related-work", "(Jones, 2021)", SOURCE_B),
    ),
    sources: Sequence[SourceCard] | None = None,
    texts: dict[str, str] | None = None,
    flagged: Sequence[str] = (),
) -> ArticleView:
    """A view that passes every gate unless a test changes one of its inputs.

    ``citation_keys`` is ``(section_id, marker, source_id)``; the marker must match the
    brief's citation style for the style gate to pass.
    """
    by_section: dict[str, list[CitationRef]] = {}
    for section_id, marker, source_id in citation_keys:
        by_section.setdefault(section_id, []).append(CitationRef(key=marker, source_id=source_id))
    states = [
        section(
            section_id,
            words=words,
            order=index,
            citations=by_section.get(section_id, []),
            text="" if section_id == "references" else prose(4),
        )
        for index, (section_id, words) in enumerate(layout)
    ]
    body_texts = dict(texts or {})
    for state in states:
        body_texts.setdefault(state.id, "" if state.id == "references" else prose(4))
    resolved_sources = (
        list(sources)
        if sources is not None
        else [source(SOURCE_A, suffix="a"), source(SOURCE_B, suffix="b", year=2021)]
    )
    return ArticleView.from_sections(
        brief,
        states,
        texts=body_texts,
        sources=resolved_sources,
        flagged_source_ids=flagged,
    )


def full_view(brief: Brief) -> ArticleView:
    """The canonical all-gates-green article view."""
    return article_view(brief)


def quality_report(
    *, q: int = 72, gates: dict[str, bool] | None = None, issues: int = 0
) -> QualityReport:
    """A validator report with all gates passing unless overridden."""
    resolved_gates = dict.fromkeys(GATE_NAMES, True)
    if gates:
        resolved_gates.update(gates)
    report_issues = [
        QualityIssue(
            id=f"ISSUE-test-{index:03d}",
            section_id="introduction",
            severity=IssueSeverity.HIGH,
            text="The claim in paragraph two has no supporting source card.",
        )
        for index in range(issues)
    ]
    return QualityReport(
        iter=1,
        gates=resolved_gates,
        scores={},
        Q=q,
        verified_citation_ratio=0.5,
        issues=report_issues,
    )


def plan_budget(tokens: int = 100_000, *, iteration: int = 0) -> PlanBudget:
    """A generous plan budget."""
    return PlanBudget(remaining_run_tokens=tokens, iteration=iteration)
