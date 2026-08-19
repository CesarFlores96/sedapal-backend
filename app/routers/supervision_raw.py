from fastapi import APIRouter, Body, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.authz import is_admin
from app.database import get_pool, get_supabase_pool
from app.repositories.supervision_raw import (
    get_supervision_raw,
    insert_supervision_raw,
    list_supervision_raw,
    list_supervision_raw_activity_report,
    list_supervision_raw_by_supply,
    list_supervision_raw_completed_range,
    update_supervision_raw,
)
from app.repositories.supervision_table import (
    mark_supervisions_exported,
    restore_supervision,
    soft_delete_supervision,
)
from app.supabase_auth import resolve_actor

router = APIRouter(prefix="/supervision-raw", tags=["supervision-raw"])


def _pool():
    try:
        return get_supabase_pool("supervision")
    except RuntimeError:
        return get_pool()


def parse_user_id(user_id: str | None) -> int | None:
    return int(user_id) if user_id and user_id.strip().isdigit() else None


async def _actor(
    authorization: str | None,
    user_role: str | None,
    user_id: str | None,
) -> tuple[str | None, int | None]:
    return await resolve_actor(get_pool(), authorization, user_role, parse_user_id(user_id))


class UpdatePayload(BaseModel):
    updates: dict = Field(default_factory=dict)


class InsertPayload(BaseModel):
    payload: dict = Field(default_factory=dict)


class MarkExportedPayload(BaseModel):
    supervisionIds: list[int] = Field(default_factory=list)


@router.get("/by-work-order/{num_os}")
async def get_by_work_order(
    num_os: str = Path(..., min_length=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict | None:
    role, actor_id = await _actor(authorization, user_role, user_id)
    row = await get_supervision_raw(_pool(), num_os=num_os)
    if not row:
        return None
    if not is_admin(role) and row.get("assigned_user_id") != actor_id:
        return None
    return row


@router.get("/by-id/{supervision_id}")
async def get_by_id(
    supervision_id: int = Path(..., ge=1),
    include_deleted: bool = Query(default=False, alias="includeDeleted"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict | None:
    role, actor_id = await _actor(authorization, user_role, user_id)
    row = await get_supervision_raw(_pool(), supervision_id=supervision_id, include_deleted=include_deleted)
    if not row:
        return None
    if not is_admin(role) and row.get("assigned_user_id") != actor_id:
        return None
    return row


@router.patch("/by-work-order/{num_os}")
async def patch_by_work_order(
    num_os: str = Path(..., min_length=1),
    body: UpdatePayload = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await update_supervision_raw(
        _pool(),
        body.updates,
        num_os=num_os,
        user_role=role,
        user_id=actor_id,
    )


@router.patch("/by-id/{supervision_id}")
async def patch_by_id(
    supervision_id: int = Path(..., ge=1),
    body: UpdatePayload = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await update_supervision_raw(
        _pool(),
        body.updates,
        supervision_id=supervision_id,
        user_role=role,
        user_id=actor_id,
    )


@router.post("")
async def post_insert(
    body: InsertPayload = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    _, actor_id = await _actor(authorization, user_role, user_id)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="Tu usuario no tiene un perfil operativo vinculado.")
    payload = dict(body.payload)
    payload["assigned_user_id"] = actor_id
    return await insert_supervision_raw(_pool(), payload)


@router.get("")
async def get_list(
    fec_emis: str | None = Query(default=None, alias="fecEmis"),
    deleted: bool = Query(default=False),
    limit: int = Query(default=1000, ge=1, le=1000),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await list_supervision_raw(
        _pool(),
        fec_emis_ddmmyyyy=fec_emis,
        deleted=deleted,
        user_role=role,
        user_id=actor_id,
        limit=limit,
    )


@router.get("/by-supply/{supply_code}")
async def get_by_supply(
    supply_code: str = Path(..., min_length=1),
    limit: int = Query(default=100, ge=1, le=100),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await list_supervision_raw_by_supply(
        _pool(),
        supply_code,
        user_role=role,
        user_id=actor_id,
        limit=limit,
    )


@router.get("/completed-range")
async def get_completed_range(
    start: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    only_unexported: bool = Query(default=False, alias="onlyUnexported"),
    require_num_os: bool = Query(default=False, alias="requireNumOs"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await list_supervision_raw_completed_range(
        _pool(),
        start,
        end,
        only_unexported=only_unexported,
        require_num_os=require_num_os,
        user_role=role,
        user_id=actor_id,
    )


@router.get("/activity-report")
async def get_activity_report(
    user_id_param: int = Query(..., ge=1, alias="userId"),
    month_suffix: str = Query(..., alias="monthSuffix", pattern=r"^/\d{2}/\d{4}$"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    role, actor_id = await _actor(authorization, user_role, user_id)
    # Defensa en profundidad: la web ya valida que solo un admin pueda pedir el
    # reporte de otro usuario; se repite aqui por si otro cliente llama directo.
    if not is_admin(role) and user_id_param != actor_id:
        raise HTTPException(status_code=403, detail="Solo un administrador puede ver el reporte de otra persona.")
    return await list_supervision_raw_activity_report(_pool(), user_id_param, month_suffix)


@router.post("/mark-exported")
async def post_mark_exported(
    body: MarkExportedPayload = Body(...),
) -> dict:
    await mark_supervisions_exported(_pool(), body.supervisionIds)
    return {"success": True}


@router.post("/by-id/{supervision_id}/soft-delete")
async def post_soft_delete(
    supervision_id: int = Path(..., ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    if not is_admin(role):
        raise HTTPException(status_code=403, detail="Solo un administrador puede anular una supervision.")
    await soft_delete_supervision(
        _pool(),
        supervision_id,
        deleted_by_user_id=actor_id,
        deleted_by_name=None,
    )
    return {"success": True}


@router.post("/by-id/{supervision_id}/restore")
async def post_restore(
    supervision_id: int = Path(..., ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    role, _ = await _actor(authorization, user_role, None)
    if not is_admin(role):
        raise HTTPException(status_code=403, detail="Solo un administrador puede restaurar una supervision.")
    await restore_supervision(_pool(), supervision_id)
    return {"success": True}
