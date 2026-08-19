from fastapi import APIRouter, Body, Header, Path, Query

from app.database import get_pool, get_supabase_pool
from app.supabase_auth import get_supabase_context
from app.repositories.supervisions import (
    ensure_supervision_draft,
    finalize_supervision,
    get_supervision_detail,
    list_supervision_agenda,
    save_supervision,
)


router = APIRouter(prefix="/supervisiones", tags=["supervisiones"])


def get_supervision_pool():
    try:
        return get_supabase_pool("supervision")
    except RuntimeError:
        return get_pool()


async def resolve_request_context(
    authorization: str | None,
    user_role: str | None,
    user_id: str | None,
) -> tuple[str | None, int | None]:
    supabase_context = await get_supabase_context(get_pool(), authorization)
    if supabase_context:
        return supabase_context.get("role"), supabase_context.get("legacy_user_id")
    return user_role, (int(user_id) if user_id and user_id.strip().isdigit() else None)


@router.get("")
async def get_supervision_agenda(
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    resolved_role, resolved_user_id = await resolve_request_context(
        authorization,
        user_role,
        user_id,
    )
    return await list_supervision_agenda(
        get_supervision_pool(),
        date,
        user_role=resolved_role,
        user_id=resolved_user_id,
    )


@router.get("/{work_order_number}")
async def get_supervision(
    work_order_number: str = Path(..., min_length=1, max_length=120),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict | None:
    resolved_role, resolved_user_id = await resolve_request_context(
        authorization,
        user_role,
        user_id,
    )
    return await get_supervision_detail(
        get_supervision_pool(),
        work_order_number,
        user_role=resolved_role,
        user_id=resolved_user_id,
    )


@router.post("/{work_order_number}")
async def post_supervision_draft(
    work_order_number: str = Path(..., min_length=1, max_length=120),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    resolved_role, resolved_user_id = await resolve_request_context(
        authorization,
        user_role,
        user_id,
    )
    return await ensure_supervision_draft(
        get_supervision_pool(),
        work_order_number,
        user_role=resolved_role,
        user_id=resolved_user_id,
    )


@router.patch("/{work_order_number}")
async def patch_supervision(
    work_order_number: str = Path(..., min_length=1, max_length=120),
    payload: dict = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    resolved_role, resolved_user_id = await resolve_request_context(
        authorization,
        user_role,
        user_id,
    )
    return await save_supervision(
        get_supervision_pool(),
        work_order_number,
        payload,
        user_role=resolved_role,
        user_id=resolved_user_id,
    )


@router.post("/{work_order_number}/finalize")
async def post_finalize_supervision(
    work_order_number: str = Path(..., min_length=1, max_length=120),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    resolved_role, resolved_user_id = await resolve_request_context(
        authorization,
        user_role,
        user_id,
    )
    return await finalize_supervision(
        get_supervision_pool(),
        work_order_number,
        user_role=resolved_role,
        user_id=resolved_user_id,
    )
