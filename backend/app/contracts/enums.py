"""Every closed vocabulary in CONTRACTS v1.

All enums are ``StrEnum`` so they serialise to their exact wire string and can be
compared against YAML config without adapters. Adding a member is a
CONTRACT-CHANGE-REQUEST: consumers switch exhaustively on these values.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AccountProvider",
    "AccountStatus",
    "AgentRole",
    "ArticleType",
    "BindingRole",
    "BindingStrategy",
    "ChangeStatus",
    "CitationStyle",
    "IssueSeverity",
    "LoopAction",
    "MessageRole",
    "ModelTier",
    "PatchOpType",
    "Recommendation",
    "SectionStatus",
    "VerificationStatus",
]


class ArticleType(StrEnum):
    """Brief.article_type: selects required sections in the rubric."""

    RESEARCH = "research"
    REVIEW = "review"
    POSITION = "position"
    CASE_STUDY = "case_study"


class CitationStyle(StrEnum):
    """Brief.citation_style: drives the citation-style hard gate."""

    APA7 = "APA7"
    IEEE = "IEEE"
    HARVARD = "Harvard"
    VANCOUVER = "Vancouver"


class AgentRole(StrEnum):
    """The five roles that may own a task in a Plan."""

    ARCHITECT = "architect"
    RESEARCHER = "researcher"
    WRITER = "writer"
    REVIEWER = "reviewer"
    VALIDATOR = "validator"


class BindingRole(StrEnum):
    """AgentBinding.role: the five agents, plus the two non-authoring LLM roles."""

    PLANNER = "planner"
    ARCHITECT = "architect"
    RESEARCHER = "researcher"
    WRITER = "writer"
    REVIEWER = "reviewer"
    VALIDATOR = "validator"
    COMPACTOR = "compactor"


class ModelTier(StrEnum):
    """Policy tier used to pick a model for a role (see config/policies.yaml)."""

    STRONG = "strong"
    MID = "mid"
    CHEAP = "cheap"


class PatchOpType(StrEnum):
    """Patch operation verbs. Payload rules live in contracts.patch."""

    REPLACE_TEXT = "replace_text"
    INSERT_AFTER = "insert_after"
    DELETE_TEXT = "delete_text"
    REPLACE_SECTION = "replace_section"


class ChangeStatus(StrEnum):
    """ChangeEntry.status. ``needs_user_input`` is the HARD RULE 1 escape hatch."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_USER_INPUT = "needs_user_input"


class SectionStatus(StrEnum):
    """SectionState.status lifecycle: empty -> draft -> reviewed -> final."""

    EMPTY = "empty"
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FINAL = "final"


class IssueSeverity(StrEnum):
    """Severity of a quality issue. ``high`` blocks completion."""

    LOW = "low"
    MED = "med"
    HIGH = "high"


class VerificationStatus(StrEnum):
    """SourceCard.verification. ``unknown`` is never silently upgraded."""

    UNKNOWN = "unknown"
    VERIFIED = "verified"
    MISMATCH = "mismatch"


class Recommendation(StrEnum):
    """Verdict.recommendation: the Planner's opinion on stopping."""

    FINISH = "finish"
    CONTINUE = "continue"


class LoopAction(StrEnum):
    """LoopDecision.action: the only values the loop controller may emit."""

    CONTINUE = "CONTINUE"
    OFFER_FINISH = "OFFER_FINISH"
    ASK_USER = "ASK_USER"
    STOP_EROSION = "STOP_EROSION"
    STOP_BUDGET = "STOP_BUDGET"
    STOP_ERRORS = "STOP_ERRORS"
    STOP_USER = "STOP_USER"


class AccountProvider(StrEnum):
    """Account.provider. Only ``mock`` is implemented in this step."""

    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"
    MOCK = "mock"


class AccountStatus(StrEnum):
    """Account.status: resolved at call time, never cached in a binding."""

    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class BindingStrategy(StrEnum):
    """How a binding picks among its accounts at call time."""

    DEDICATED = "dedicated"
    ROUND_ROBIN = "round_robin"
    FAILOVER = "failover"


class MessageRole(StrEnum):
    """Roles permitted in LLMRequest.messages. System text lives in system_blocks."""

    USER = "user"
    ASSISTANT = "assistant"
