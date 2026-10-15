"""HTTP surface. FastAPI app factory plus routers; no orchestration logic lives here.

Later steps add ``/api/runs``, ``/api/accounts`` and the SSE progress stream. Until
then this package exposes liveness, effective configuration and the contract schemas -
enough for the UI to be wired up and for a human to see which policies are in force.
"""

from __future__ import annotations

from app.api.deps import PoliciesDep, RubricDep, SettingsDep
from app.api.main import create_app

__all__ = ["PoliciesDep", "RubricDep", "SettingsDep", "create_app"]
