"""Agent-side code.

Today this package holds the *context* an agent is given: the bounded prompt
built from the brief, the task and references (see :mod:`app.agents.context`).

The agent runtime itself - the generic structured-output loop, the role registry
and the concrete architect/researcher/writer/reviewer/validator agents - arrives
in a later step. The context builder is written against that runtime's seam
(``build(task, run_state) -> LLMRequest``) and does not depend on it, so the two
can land in either order.
"""

from __future__ import annotations

from app.agents.context import ContextError, TaskContext, TaskContextBuilder

__all__ = ["ContextError", "TaskContext", "TaskContextBuilder"]
