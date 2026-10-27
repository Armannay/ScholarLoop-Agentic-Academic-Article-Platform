"""The five role agents: architect, researcher, writer, reviewer, validator.

Each is a :class:`~app.agents.roles.base.RoleAgent` on the Step 5 runtime, so they all
inherit the same execution path (context, governor precheck, one JSON-constrained call,
contract validation, at most one repair, measured usage) and differ only in what the
deliverable says a role is:

================  =======  ============  ============================================
role              tier     tools         deliverable
================  =======  ============  ============================================
architect         mid      -             ordered section seeds + goals/claims/words
researcher        mid      scholar_search up to 12 SourceCards + a finding each
writer            mid      -             one patch for one section, cited
reviewer          strong   -             <= 8 issues per dirty section + scores
validator         cheap    text_checks,  QualityReport: 8 gates from tools, Q from the
                           citation_check reviewer's scores
                           doi_verify
================  =======  ============  ============================================

Everything in this package is importable from ``app.agents.roles``; the module split is
implementation detail (one file per role, plus the shared base, schemas and helpers).
"""

from app.agents.roles.architect import ArchitectAgent
from app.agents.roles.base import (
    ROLE_PROMPT_DIR,
    RoleAgent,
    RolePromptContextBuilder,
    RoleRun,
    load_role_prompt,
    repository_root,
    role_call_schema,
    role_prompt_path,
)
from app.agents.roles.errors import RoleTaskInvalid
from app.agents.roles.factory import (
    ROLE_AGENTS,
    build_role_agent,
    default_role_context_builder,
    register_role_agents,
)
from app.agents.roles.golden import GoldenCase, load_golden, load_goldens, scripted_answer
from app.agents.roles.inputs import (
    EmptySectionTexts,
    MappingSectionTargets,
    MappingSectionTexts,
    NoReportSource,
    ReportSource,
    ReviewScoreboard,
    RubricSectionTargets,
    SectionScore,
    SectionTargetProvider,
    SectionTextProvider,
    StaticReportSource,
)
from app.agents.roles.quality import (
    BASELINE_SCORE,
    GatePack,
    figure_and_table_gate,
    merge_scores,
    run_deterministic_gates,
    weighted_q,
)
from app.agents.roles.researcher import ResearcherAgent
from app.agents.roles.reviewer import ReviewerAgent
from app.agents.roles.schemas import (
    MAX_FINDINGS,
    MAX_ISSUES_PER_SECTION,
    MAX_SUGGESTED_FIX_WORDS,
    ArchitectOutput,
    Finding,
    ResearcherOutput,
    ReviewerOutput,
    ReviewIssue,
    SectionReview,
    SectionSeed,
    SubjectiveChecks,
    ValidatorOutput,
    WriterOutput,
)
from app.agents.roles.validator import ValidatorAgent
from app.agents.roles.writer import WriterAgent

__all__ = [
    "BASELINE_SCORE",
    "MAX_FINDINGS",
    "MAX_ISSUES_PER_SECTION",
    "MAX_SUGGESTED_FIX_WORDS",
    "ROLE_AGENTS",
    "ROLE_PROMPT_DIR",
    "ArchitectAgent",
    "ArchitectOutput",
    "EmptySectionTexts",
    "Finding",
    "GatePack",
    "GoldenCase",
    "MappingSectionTargets",
    "MappingSectionTexts",
    "NoReportSource",
    "ReportSource",
    "ResearcherAgent",
    "ResearcherOutput",
    "ReviewIssue",
    "ReviewScoreboard",
    "ReviewerAgent",
    "ReviewerOutput",
    "RoleAgent",
    "RolePromptContextBuilder",
    "RoleRun",
    "RoleTaskInvalid",
    "RubricSectionTargets",
    "SectionReview",
    "SectionScore",
    "SectionSeed",
    "SectionTargetProvider",
    "SectionTextProvider",
    "StaticReportSource",
    "SubjectiveChecks",
    "ValidatorAgent",
    "ValidatorOutput",
    "WriterAgent",
    "WriterOutput",
    "build_role_agent",
    "default_role_context_builder",
    "figure_and_table_gate",
    "load_golden",
    "load_goldens",
    "load_role_prompt",
    "merge_scores",
    "register_role_agents",
    "repository_root",
    "role_call_schema",
    "role_prompt_path",
    "run_deterministic_gates",
    "scripted_answer",
    "weighted_q",
]
