"""Generic agent runtime and role registry.

Concrete role-specific agents are registered by the later agent step.
"""

from __future__ import annotations

from app.agents.base import Agent, AgentOutput
from app.agents.registry import (
    AgentDisabledError,
    AgentRegistry,
    UnknownAgentRoleError,
)

__all__ = [
    "Agent",
    "AgentDisabledError",
    "AgentOutput",
    "AgentRegistry",
    "UnknownAgentRoleError",
]
