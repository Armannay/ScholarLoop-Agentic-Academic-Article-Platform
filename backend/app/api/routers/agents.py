"""Per-role agent bindings: which accounts and model each role uses (planner included)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.accounts.schemas import BindingPut, BindingView
from app.api.deps import BindingServiceDep, CurrentUserDep, SessionDep
from app.contracts.enums import BindingRole

__all__ = ["router"]

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/{role}/binding", response_model=BindingView, summary="Binding for a role")
def get_binding(
    role: BindingRole, user: CurrentUserDep, bindings: BindingServiceDep
) -> BindingView:
    view = bindings.get(user.id, role)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"no binding for role {role.value!r}"},
        )
    return view


@router.put("/{role}/binding", response_model=BindingView, summary="Create or replace a binding")
def put_binding(
    role: BindingRole,
    payload: BindingPut,
    user: CurrentUserDep,
    bindings: BindingServiceDep,
    session: SessionDep,
) -> BindingView:
    view = bindings.put(user.id, role, payload)
    session.commit()
    return view
