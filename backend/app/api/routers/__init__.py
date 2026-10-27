"""API routers. One module per resource group; all prefixed under /api."""

from __future__ import annotations

from app.api.routers import config, health, projects

__all__ = ["config", "health", "projects"]
