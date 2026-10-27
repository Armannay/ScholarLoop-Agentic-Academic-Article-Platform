"""Request/response models for the projects API surface.

These are *surface* models, like :mod:`app.accounts.schemas`: they describe the HTTP
payloads without changing the frozen contracts. ``brief`` is typed as a plain JSON
object on purpose - the request is validated by :mod:`app.projects.validation` so that
a bad brief comes back as a per-field 422 naming ``brief.target_words`` rather than as
one opaque "invalid body".

Shape notes, and where they stand relative to ``docs/openapi/openapi.yaml``:

* ``ProjectView`` keeps the spec's fields (``id``, ``name``, ``description``,
  ``run_count``, ``latest_*``, ``best_q``, timestamps) and adds the stored project
  record (``brief``, ``policy_overrides``, ``status``, ``owner_id``). The spec's
  projects section predates the project table and is marked ``planned`` there; the
  delta is logged as a spec-change request in ``docs/arena-log/step-11``.
* ``ProjectList`` adds ``next_cursor``/``has_more`` for keyset pagination. ``total``
  stays, so a console can still render "3 of 12".
* ``id`` is a ULID, not the slug the spec proposed: the spec itself asks for a real
  ``Project`` record (CONTRACT-CHANGE-REQUEST 7) and every other row in the database is
  a ULID. A human-readable label is ``name``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from app.contracts.brief import Brief
from app.contracts.common import Contract, NonEmptyStr, Ulid

__all__ = [
    "ProjectCreate",
    "ProjectList",
    "ProjectPatch",
    "ProjectStatus",
    "ProjectView",
]


class ProjectStatus(StrEnum):
    """Project lifecycle. ``archived`` keeps the record readable but out of the way."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectCreate(Contract):
    """POST /api/projects.

    ``brief`` is required: a project *is* the brief it will write against, and every
    run of the project inherits it. ``policy_overrides`` is optional and may be empty.
    """

    name: NonEmptyStr = Field(max_length=120, description="Display name, unique per owner.")
    description: str | None = Field(default=None, max_length=600)
    brief: dict[str, Any] = Field(
        description="CONTRACTS v1 Brief (validated field by field; never echoed back)."
    )
    policy_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Partial policy block, validated through app.core.policies.",
    )
    status: ProjectStatus = Field(default=ProjectStatus.ACTIVE)


class ProjectPatch(Contract):
    """PATCH /api/projects/{project_id}. Every member is optional.

    ``model_fields_set`` distinguishes "not sent" (leave as is) from an explicit
    ``null``: ``description: null`` clears the description and ``policy_overrides:
    null`` clears the overrides. ``name`` and ``brief`` cannot be cleared - a project
    without either is not a project.
    """

    name: NonEmptyStr | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=600)
    brief: dict[str, Any] | None = None
    policy_overrides: dict[str, Any] | None = None
    status: ProjectStatus | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> ProjectPatch:
        if not self.model_fields_set:
            raise ValueError("provide at least one field to change")
        return self


class ProjectView(Contract):
    """A project as the API exposes it, with run facts derived from its runs.

    ``run_count`` and the ``latest_*``/``best_q`` fields are computed through
    :class:`app.projects.runs.RunsQuery`, never stored on the project row. A project
    with no runs answers zeros and nulls, which is what the console renders on the
    first day.
    """

    id: Ulid
    owner_id: NonEmptyStr
    name: NonEmptyStr
    description: str | None = None
    brief: Brief
    policy_overrides: dict[str, Any] = Field(default_factory=dict)
    status: ProjectStatus
    run_count: int = Field(default=0, ge=0)
    latest_run_id: Ulid | None = None
    latest_state: str | None = None
    latest_q: int | None = Field(default=None, ge=0, le=100)
    best_q: float | None = None
    created_at: datetime
    updated_at: datetime


class ProjectList(Contract):
    """GET /api/projects - one keyset page plus the owner's total.

    ``next_cursor`` is opaque: clients pass it back verbatim as ``?cursor=``. It is
    null on the last page, so ``has_more`` is only there to save a null check.
    """

    projects: list[ProjectView] = Field(default_factory=list)
    total: int = Field(ge=0)
    next_cursor: str | None = None
    has_more: bool = False
