"""Package marker for app.core. Import-free on purpose: submodules are addressed
explicitly (``from app.core.policies import load_policies``) so that importing
one primitive never drags in the whole config tree.
"""

from __future__ import annotations

__all__ = ["clock", "errors", "ids", "lifecycle", "logging", "policies", "rubric", "settings"]
