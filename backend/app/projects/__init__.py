"""Projects: the owner-scoped container a run belongs to (Step 11).

A project is a name, the ``Brief`` it writes against, the policy overrides that apply
to its runs, a status and timestamps - everything the console needs before the first
run exists. It is stored (``projects`` table, Alembic revision ``0002_projects``) and
exposed by ``/api/projects``; it is not a CONTRACTS v1 document, so the frozen wire
contracts are untouched.

Module map:

* :mod:`app.projects.models` - the SQLAlchemy row;
* :mod:`app.projects.schemas` - request/response surface models;
* :mod:`app.projects.validation` - ``Brief`` and policy-override validation, mapped to
  per-field 422 errors;
* :mod:`app.projects.pagination` - opaque keyset cursors;
* :mod:`app.projects.runs` - the ``RunsQuery`` Protocol, an in-memory fake and the
  read-only adapter over the persisted run store;
* :mod:`app.projects.service` - CRUD, ownership and the active-run guard.
"""

from __future__ import annotations

from app.projects.models import ProjectRow
from app.projects.pagination import ProjectCursor, decode_cursor, encode_cursor
from app.projects.runs import (
    ActiveRun,
    InMemoryRunsQuery,
    RunFacts,
    RunsQuery,
    RunSummary,
    StoreRunsQuery,
)
from app.projects.schemas import (
    ProjectCreate,
    ProjectList,
    ProjectPatch,
    ProjectStatus,
    ProjectView,
)
from app.projects.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ProjectPage,
    ProjectService,
)
from app.projects.validation import (
    ProjectValidationError,
    validate_brief,
    validate_policy_overrides,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ActiveRun",
    "InMemoryRunsQuery",
    "ProjectCreate",
    "ProjectCursor",
    "ProjectList",
    "ProjectPage",
    "ProjectPatch",
    "ProjectRow",
    "ProjectService",
    "ProjectStatus",
    "ProjectValidationError",
    "ProjectView",
    "RunFacts",
    "RunSummary",
    "RunsQuery",
    "StoreRunsQuery",
    "decode_cursor",
    "encode_cursor",
    "validate_brief",
    "validate_policy_overrides",
]
