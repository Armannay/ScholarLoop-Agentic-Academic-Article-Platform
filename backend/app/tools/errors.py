"""Tool error family.

Two kinds of failure are deliberately different:

* **Argument or policy errors** (:class:`ToolArgumentError`, :class:`ToolURLBlocked`)
  mean the caller asked for something it may not have. They raise, because silently
  returning empty data would teach an agent that a refused request was a miss.
* **Transport and availability errors** (:class:`HTTPFetchError`,
  :class:`HostResolutionError`) are normal facts about the network. Concrete tools
  convert them into a structured ``status`` envelope so the loop can continue and
  the agent can choose another route.

Nothing in this module carries key material or raw tool output; messages are short,
machine-readable reasons safe to put in a ledger entry.
"""

from __future__ import annotations

__all__ = [
    "HTTPFetchError",
    "HostResolutionError",
    "ToolArgumentError",
    "ToolError",
    "ToolURLBlocked",
]


class ToolError(RuntimeError):
    """Base class for every tool failure."""


class ToolArgumentError(ToolError, ValueError):
    """The tool arguments do not match the tool's JSON schema."""


class ToolURLBlocked(ToolError, PermissionError):
    """A URL was refused by the egress policy (scheme, host, address or port)."""


class HostResolutionError(ToolError):
    """The hostname could not be resolved; the caller may treat this as unavailable."""


class HTTPFetchError(ToolError):
    """A request was attempted but did not produce a response (timeout, transport)."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        """Structured form used by tools in their ``unavailable`` envelope."""
        payload: dict[str, object] = {"reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload
