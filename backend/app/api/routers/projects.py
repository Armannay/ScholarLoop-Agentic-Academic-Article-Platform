"""Projects API: create, list (keyset), read, patch, delete. Owner-scoped, no secrets.

Every route requires the bearer token from Step 1 (``CurrentUserDep``). The rules a
client can rely on:

* ``POST /api/projects`` - 201, or 409 ``conflict``/``project_exists`` for a name this
  owner already uses, or 422 ``validation_error`` with ``context.errors[]`` naming each
  bad ``brief.*`` / ``policy_overrides.*`` field.
* ``GET /api/projects?limit=&cursor=`` - newest first; ``next_cursor`` is opaque and
  null on the last page; ``total`` counts the owner's whole list.
* ``GET /api/projects/{id}`` - 200 or 404. A foreign project is 404, never 403: the API
  does not confirm that someone else's id exists.
* ``PATCH`` / ``DELETE /api/projects/{id}`` - 409 ``conflict`` with
  ``context.reason=active_run`` while a run of that project is still working, so an
  edit can never race an agent mid-article. ``CHECKPOINT`` is not active - the loop is
  waiting for a human, and editing then is the whole point.
* ``DELETE`` answers 204 with no body.

Pagination is keyset, not offset: ``(created_at, id)`` with the id as tie-break, so
creating a project between two pages cannot duplicate or skip a row.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUserDep, ProjectServiceDep, SessionDep
from app.projects.schemas import ProjectCreate, ProjectList, ProjectPatch, ProjectView
from app.projects.service import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

__all__ = ["router"]

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get(
    "",
    response_model=ProjectList,
    summary="List my projects (keyset pagination)",
)
def list_projects(
    user: CurrentUserDep,
    projects: ProjectServiceDep,
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="Maximum projects to return.",
    ),
    cursor: str | None = Query(
        default=None,
        max_length=512,
        description="`next_cursor` from the previous page; omit for the first page.",
    ),
) -> ProjectList:
    """One page of the caller's projects, newest first."""
    page = projects.list_page(user.id, limit=limit, cursor=cursor)
    return ProjectList(
        projects=page.projects,
        total=page.total,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "",
    response_model=ProjectView,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project (brief validated against CONTRACTS v1)",
)
def create_project(
    payload: ProjectCreate,
    user: CurrentUserDep,
    projects: ProjectServiceDep,
    session: SessionDep,
) -> ProjectView:
    """Validate the brief and policy overrides, then reserve the project."""
    view = projects.create(user.id, payload)
    session.commit()
    return view


@router.get("/{project_id}", response_model=ProjectView, summary="One project")
def get_project(project_id: str, user: CurrentUserDep, projects: ProjectServiceDep) -> ProjectView:
    """The caller's project, or 404 (foreign ids read as missing)."""
    return projects.get(user.id, project_id)


@router.patch(
    "/{project_id}",
    response_model=ProjectView,
    summary="Update a project (409 while a run is active)",
)
def patch_project(
    project_id: str,
    payload: ProjectPatch,
    user: CurrentUserDep,
    projects: ProjectServiceDep,
    session: SessionDep,
) -> ProjectView:
    """Partially update a project; refused with 409 while a run is working."""
    view = projects.patch(user.id, project_id, payload)
    session.commit()
    return view


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a project (409 while a run is active)",
)
def delete_project(
    project_id: str,
    user: CurrentUserDep,
    projects: ProjectServiceDep,
    session: SessionDep,
) -> Response:
    """Delete a project; refused with 409 while a run is working."""
    projects.delete(user.id, project_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
