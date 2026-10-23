"""CONTRACTS v1 for ScholarLoop.

One module per contract entry plus :mod:`app.contracts.enums` (closed vocabularies)
and :mod:`app.contracts.common` (shared types and base classes). All models are
Pydantic v2 with ``extra="forbid"``; document-level models carry
``schema_version: Literal[1]``.

This package is the single source of truth for the wire format. It must not import
anything from ``orchestrator``, ``api`` or ``agents`` - contracts are the bottom of
the dependency graph, in both senses.
"""

from __future__ import annotations

from app.contracts.account import Account, AccountPublic, MonthlyBudget, key_fingerprint
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.common import SCHEMA_VERSION, Contract, VersionedContract, word_count
from app.contracts.enums import (
    AccountProvider,
    AccountStatus,
    AgentRole,
    ArticleType,
    BindingRole,
    BindingStrategy,
    ChangeStatus,
    CitationStyle,
    IssueSeverity,
    LoopAction,
    MessageRole,
    ModelTier,
    PatchOpType,
    Recommendation,
    SectionStatus,
    VerificationStatus,
)
from app.contracts.ledger import ChangeEntry, TokenUsage, patch_ref_for
from app.contracts.llm import LLMMessage, LLMRequest, LLMResponse, SystemBlock
from app.contracts.loop import STOP_ACTIONS, LoopDecision
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import REQUIRED_ROLES, Plan, PlanConflict
from app.contracts.quality import GATE_NAMES, QualityIssue, QualityReport
from app.contracts.registry import (
    CONTRACT_NAMES,
    CONTRACTS,
    JSON_SCHEMA_DIALECT,
    get_contract,
    json_schema_for,
)
from app.contracts.section import CitationRef, SectionState
from app.contracts.source import SourceCard, normalise_doi
from app.contracts.task import Task
from app.contracts.verdict import Verdict

__all__ = [
    "CONTRACTS",
    "CONTRACT_NAMES",
    "GATE_NAMES",
    "JSON_SCHEMA_DIALECT",
    "REQUIRED_ROLES",
    "SCHEMA_VERSION",
    "STOP_ACTIONS",
    "Account",
    "AccountProvider",
    "AccountPublic",
    "AccountStatus",
    "AgentBinding",
    "AgentResult",
    "AgentRole",
    "ArticleType",
    "BindingRole",
    "BindingStrategy",
    "Brief",
    "ChangeEntry",
    "ChangeStatus",
    "CitationRef",
    "CitationStyle",
    "Contract",
    "IssueSeverity",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "LoopAction",
    "LoopDecision",
    "MessageRole",
    "ModelTier",
    "MonthlyBudget",
    "Patch",
    "PatchOp",
    "PatchOpType",
    "Plan",
    "PlanConflict",
    "QualityIssue",
    "QualityReport",
    "Recommendation",
    "SectionState",
    "SectionStatus",
    "SourceCard",
    "SystemBlock",
    "Task",
    "TokenUsage",
    "Verdict",
    "VerificationStatus",
    "VersionedContract",
    "get_contract",
    "json_schema_for",
    "key_fingerprint",
    "normalise_doi",
    "patch_ref_for",
    "word_count",
]
