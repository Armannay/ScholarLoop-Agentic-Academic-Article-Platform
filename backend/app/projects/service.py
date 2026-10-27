"""Project CRUD: ownership, validation, keyset pagination and the active-run guard.

One session, one owner, one mutation per method - the same discipline as
:mod:`app.accounts.service`. The rules this module exists to enforce:

* **Ownership is per user and reads as 404.** A foreign project is indistinguishable
  from a missing one: the API never confirms that another user's id exists.
* **The brief and the policy overrides are validated by their owners**, not here:
  :func:`app.projects.validation.validate_brief` hands the brief to the frozen
  ``Brief`` contract, and ``validate_policy_overrides`` to ``app.core.policies``. A
  bad payload is a 422 with per-field errors *before* anything is written.
* **A project with a live run is not editable.** PATCH and DELETE ask
  :class:`app.projects.runs.RunsQuery` first and answer 409 ``conflict`` with
  ``context.reason=active_run`` - the console renders "stop the run first", and a
  half-edited brief can never race an agent. ``CHECKPOINT`` is not active: the loop is
  waiting for the user, so editing then is legal.
* **No secrets reach the audit trail or the logs.** Audit detail records *which*
  fields changed (``["brief", "name"]``), never their values; the project name and
  status are the only values logged at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounts.service import AuditService
from app.contracts.brief import Brief
from app.core.clock import to_utc, utc_now
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import new_ulid
from app.core.logging import get_logger
from app.projects.models import ProjectRow
from app.projects.pagination import ProjectCursor, decode_cursor, encode_cursor
from app.projects.runs import RunsQuery, RunSummary
from app.projects.schemas import ProjectCreate, ProjectPatch, ProjectStatus, ProjectView
from app.projects.validation import (
    ProjectValidationError,
    policy_override_keys,
    validate_brief,
    validate_policy_overrides,
)

__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "ProjectPage", "ProjectService"]

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_\-=]*$")


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; every wire timestamp is aware UTC."""
    return to_utc(value)


@dataclass(frozen=True)
class ProjectPage:
    """One page of projects plus the cursor to continue from."""

    projects: list[ProjectView]
    total: int
    next_cursor: str | None
    has_more: bool


class ProjectService:
    """CRUD for one owner's projects. Callers commit; the service flushes."""

    def __init__(
        self,
        session: Session,
        runs: RunsQuery,
        audit: AuditService | None = None,
    ) -> None:
        self._session = session
        self._runs = runs
        self._audit = audit if audit is not None else AuditService(session)

    # -- reads --------------------------------------------------------------
    def list_page(
        self,
        owner_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> ProjectPage:
        """Newest-first keyset page of the owner's projects.

        Args:
            owner_id: the caller; every query is scoped to it.
            limit: page size, clamped to ``1..MAX_PAGE_SIZE``.
            cursor: the previous page's ``next_cursor``, or None for the first page.

        Raises:
            ProjectValidationError: 422 when ``cursor`` is not a cursor this API issued.
        """
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        statement = select(ProjectRow).where(ProjectRow.owner_id == owner_id)
        if cursor is not None:
            after = self._parse_cursor(cursor)
            statement = statement.where(
                or_(
                    ProjectRow.created_at < after.created_at,
                    and_(
                        ProjectRow.created_at == after.created_at,
                        ProjectRow.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(ProjectRow.created_at.desc(), ProjectRow.id.desc()).limit(
            page_size + 1
        )
        rows = list(self._session.scalars(statement).all())
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        next_cursor = (
            encode_cursor(_aware(page_rows[-1].created_at), page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        total = int(
            self._session.scalar(
                select(func.count()).select_from(ProjectRow).where(ProjectRow.owner_id == owner_id)
            )
            or 0
        )
        # One pass over the run store for the whole page, not one per project.
        facts = self._runs.summaries(owner_id, [row.id for row in page_rows])
        return ProjectPage(
            projects=[self.to_view(row, facts.get(row.id, RunSummary())) for row in page_rows],
            total=total,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get(self, owner_id: str, project_id: str) -> ProjectView:
        """One project, or 404 - including for another owner's project."""
        return self.to_view(self._require(owner_id, project_id))

    # -- writes -------------------------------------------------------------
    def create(self, owner_id: str, payload: ProjectCreate) -> ProjectView:
        """Validate and insert a project.

        Raises:
            ProjectValidationError: 422 when the brief or the policy overrides are invalid.
            ConflictError: 409 ``project_exists`` when this owner already uses the name.
        """
        brief = validate_brief(payload.brief)
        overrides = validate_policy_overrides(payload.policy_overrides)
        name = payload.name
        if self._row_by_name(owner_id, name) is not None:
            raise self._name_conflict(name)

        now = utc_now()
        row = ProjectRow(
            id=new_ulid(),
            owner_id=owner_id,
            name=name,
            description=payload.description,
            brief=brief,
            policy_overrides=overrides,
            status=payload.status.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # A concurrent create won the (owner_id, name) race after the check above.
            self._session.rollback()
            raise self._name_conflict(name) from exc
        self._audit.record(
            actor=owner_id,
            action="project.create",
            target=row.id,
            detail={
                "name": name,
                "status": row.status,
                "policy_keys": policy_override_keys(overrides),
            },
        )
        logger.info(
            "project created",
            extra={"project_id": row.id, "owner_id": owner_id, "status": row.status},
        )
        return self.to_view(row)

    def patch(self, owner_id: str, project_id: str, payload: ProjectPatch) -> ProjectView:
        """Apply a partial update, refusing while a run is active.

        Raises:
            NotFoundError: 404 for a missing or foreign project.
            ConflictError: 409 ``active_run`` while the project has a running run.
            ProjectValidationError: 422 for an invalid brief/policy block or a null name/brief.
        """
        row = self._require(owner_id, project_id)
        self._refuse_while_active(owner_id, project_id, action="updated")
        fields = payload.model_fields_set
        changed: list[str] = []

        if "name" in fields:
            if payload.name is None:
                raise ProjectValidationError(
                    [{"loc": "name", "msg": "name cannot be null", "type": "value_error"}]
                )
            if payload.name != row.name and self._row_by_name(owner_id, payload.name) is not None:
                raise self._name_conflict(payload.name)
            row.name = payload.name
            changed.append("name")

        if "description" in fields:
            row.description = payload.description
            changed.append("description")

        if "brief" in fields:
            if payload.brief is None:
                raise ProjectValidationError(
                    [{"loc": "brief", "msg": "brief cannot be null", "type": "value_error"}]
                )
            row.brief = validate_brief(payload.brief)
            changed.append("brief")

        if "policy_overrides" in fields:
            row.policy_overrides = validate_policy_overrides(payload.policy_overrides)
            changed.append("policy_overrides")

        if "status" in fields:
            if payload.status is None:
                raise ProjectValidationError(
                    [{"loc": "status", "msg": "status cannot be null", "type": "value_error"}]
                )
            row.status = payload.status.value
            changed.append("status")

        renamed_to = row.name
        row.updated_at = utc_now()
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._name_conflict(renamed_to) from exc
        self._audit.record(
            actor=owner_id,
            action="project.patch",
            target=row.id,
            detail={"fields": changed},  # field names only - never values
        )
        logger.info(
            "project updated",
            extra={"project_id": row.id, "owner_id": owner_id, "fields": changed},
        )
        return self.to_view(row)

    def delete(self, owner_id: str, project_id: str) -> None:
        """Delete a project, refusing while a run is active.

        Raises:
            NotFoundError: 404 for a missing or foreign project.
            ConflictError: 409 ``active_run`` while the project has a running run.
        """
        row = self._require(owner_id, project_id)
        self._refuse_while_active(owner_id, project_id, action="deleted")
        self._session.delete(row)
        self._session.flush()
        self._audit.record(
            actor=owner_id,
            action="project.delete",
            target=project_id,
            detail={"name": row.name, "status": row.status},
        )
        logger.info(
            "project deleted",
            extra={"project_id": project_id, "owner_id": owner_id},
        )

    # -- mapping ------------------------------------------------------------
    def to_view(self, row: ProjectRow, summary: RunSummary | None = None) -> ProjectView:
        """Row -> wire view, with run facts derived through ``RunsQuery``.

        ``summary`` lets a page pass the facts it already fetched; a single read lets
        the method ask the seam itself.
        """
        facts = summary if summary is not None else self._runs.summary(row.owner_id, row.id)
        return ProjectView(
            id=row.id,
            owner_id=row.owner_id,
            name=row.name,
            description=row.description,
            brief=Brief.model_validate(row.brief),
            policy_overrides=dict(row.policy_overrides or {}),
            status=ProjectStatus(row.status),
            run_count=facts.run_count,
            latest_run_id=facts.latest_run_id,
            latest_state=facts.latest_state,
            latest_q=facts.latest_q,
            best_q=facts.best_q,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    # -- internals ----------------------------------------------------------
    def _require(self, owner_id: str, project_id: str) -> ProjectRow:
        row = self._session.get(ProjectRow, project_id)
        if row is None or row.owner_id != owner_id:
            raise NotFoundError(f"project {project_id} not found", project_id=project_id)
        return row

    def _row_by_name(self, owner_id: str, name: str) -> ProjectRow | None:
        return self._session.scalar(
            select(ProjectRow).where(ProjectRow.owner_id == owner_id, ProjectRow.name == name)
        )

    @staticmethod
    def _name_conflict(name: str) -> ConflictError:
        return ConflictError(
            f"a project named {name!r} already exists",
            reason="project_exists",
            name=name,
        )

    def _refuse_while_active(self, owner_id: str, project_id: str, *, action: str) -> None:
        active = self._runs.active_run(owner_id, project_id)
        if active is None:
            return
        raise ConflictError(
            f"project {project_id} cannot be {action}: run {active.id} is {active.state}; "
            "stop the run first",
            reason="active_run",
            project_id=project_id,
            run_id=active.id,
            state=active.state,
        )

    @staticmethod
    def _parse_cursor(cursor: str) -> ProjectCursor:
        text = cursor.strip()
        if not _CURSOR_RE.match(text):
            raise ProjectValidationError(
                [{"loc": "cursor", "msg": "cursor is not valid base64url", "type": "value_error"}]
            )
        try:
            return decode_cursor(text)
        except ValueError as exc:
            raise ProjectValidationError(
                [{"loc": "cursor", "msg": str(exc), "type": "value_error"}]
            ) from exc
