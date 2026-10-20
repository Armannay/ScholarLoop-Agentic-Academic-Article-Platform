"""Generic agent runtime: context, governance seam, one repair, typed registry.

Step 5 ships the machinery only. The five concrete agents (Step 7) subclass
:class:`~app.agents.base.Agent`, set ``role`` and register their factory; they inherit
this execution path unchanged.
"""

from __future__ import annotations

from app.agents.base import (
    AGENT_RESULT_SCHEMA,
    MAX_VALIDATION_ERRORS,
    REPAIR_MAX_OUTPUT_CHARS,
    REPAIR_SYSTEM_PROMPT,
    Agent,
    AgentProvider,
    BindingResolver,
    Governor,
    agent_output_schema,
    parse_agent_json,
)
from app.agents.binding import (
    BindingStore,
    CallableBindingResolver,
    MappingBindingResolver,
    UserBindingResolver,
)
from app.agents.context import (
    TASK_INSTRUCTION,
    AgentContext,
    AgentRunState,
    ContextBuilder,
    DefaultContextBuilder,
    RunState,
    load_role_rules,
)
from app.agents.errors import (
    AgentDisabled,
    AgentError,
    AgentNotRegistered,
    AgentOutputInvalid,
    AgentRegistrationError,
    AgentTaskMismatch,
)
from app.agents.governor import CallBudgetGovernor
from app.agents.pool import PoolAgentProvider, PoolLike
from app.agents.registry import AgentFactory, AgentRegistry

__all__ = [
    "AGENT_RESULT_SCHEMA",
    "MAX_VALIDATION_ERRORS",
    "REPAIR_MAX_OUTPUT_CHARS",
    "REPAIR_SYSTEM_PROMPT",
    "TASK_INSTRUCTION",
    "Agent",
    "AgentContext",
    "AgentDisabled",
    "AgentError",
    "AgentFactory",
    "AgentNotRegistered",
    "AgentOutputInvalid",
    "AgentProvider",
    "AgentRegistrationError",
    "AgentRegistry",
    "AgentRunState",
    "AgentTaskMismatch",
    "BindingResolver",
    "BindingStore",
    "CallBudgetGovernor",
    "CallableBindingResolver",
    "ContextBuilder",
    "DefaultContextBuilder",
    "Governor",
    "MappingBindingResolver",
    "PoolAgentProvider",
    "PoolLike",
    "RunState",
    "UserBindingResolver",
    "agent_output_schema",
    "load_role_rules",
    "parse_agent_json",
]
