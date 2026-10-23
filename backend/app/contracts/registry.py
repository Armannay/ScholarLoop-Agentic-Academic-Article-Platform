"""The contract registry: name -> model, in one place.

Everything that must stay in sync with the contracts (schema export, contract
round-trip tests, the `contracts` API endpoint) reads this mapping instead of
hard-coding class names. Add a contract here and it is immediately exported.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel

from app.contracts.account import Account, AccountPublic, MonthlyBudget
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.llm import LLMMessage, LLMRequest, LLMResponse, SystemBlock
from app.contracts.loop import LoopDecision
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import QualityIssue, QualityReport
from app.contracts.section import CitationRef, SectionState
from app.contracts.source import SourceCard
from app.contracts.task import Task
from app.contracts.verdict import Verdict

__all__ = [
    "CONTRACTS",
    "CONTRACT_NAMES",
    "JSON_SCHEMA_DIALECT",
    "get_contract",
    "json_schema_for",
]

#: The dialect every exported schema is written in. Pydantic v2 emits 2020-12
#: structures (``$defs``, ``prefixItems``), so say so instead of leaving consumers
#: to guess from the shape.
JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

#: Ordered so the exported schemas read top-down: intake -> planning -> execution
#: -> state -> evaluation -> decisions -> providers.
CONTRACTS: Final[dict[str, type[BaseModel]]] = {
    # intake
    "Brief": Brief,
    # planning
    "Task": Task,
    "Plan": Plan,
    # execution
    "SourceCard": SourceCard,
    "Patch": Patch,
    "PatchOp": PatchOp,
    "ChangeEntry": ChangeEntry,
    "TokenUsage": TokenUsage,
    "AgentResult": AgentResult,
    # state
    "SectionState": SectionState,
    "CitationRef": CitationRef,
    # evaluation
    "QualityReport": QualityReport,
    "QualityIssue": QualityIssue,
    "Verdict": Verdict,
    "LoopDecision": LoopDecision,
    # accounts and bindings
    "Account": Account,
    "AccountPublic": AccountPublic,
    "MonthlyBudget": MonthlyBudget,
    "AgentBinding": AgentBinding,
    # providers
    "LLMRequest": LLMRequest,
    "LLMResponse": LLMResponse,
    "SystemBlock": SystemBlock,
    "LLMMessage": LLMMessage,
}

CONTRACT_NAMES: Final[tuple[str, ...]] = tuple(CONTRACTS)


def get_contract(name: str) -> type[BaseModel]:
    """Look up a contract model by name, or raise KeyError with the valid names."""
    try:
        return CONTRACTS[name]
    except KeyError as exc:  # pragma: no cover - exercised via the API test
        raise KeyError(f"unknown contract {name!r}; known: {', '.join(CONTRACT_NAMES)}") from exc


def json_schema_for(name: str) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for one contract, with a stable ``$id``."""
    model = get_contract(name)
    schema: dict[str, Any] = model.model_json_schema(mode="validation")
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = f"https://scholarloop.local/schemas/v1/{name}.json"
    schema["title"] = name
    return schema
