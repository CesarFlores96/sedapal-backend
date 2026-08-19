from fastapi import HTTPException
from psycopg_pool import AsyncConnectionPool

from app.repositories.auth import get_access_snapshot


def normalize_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"admin", "superadmin"}:
        return "admin"
    if normalized in {"operator", "supervisor"}:
        return "supervisor"
    if normalized == "consultor":
        return "consultor"
    return "supervisor"


def is_admin(role: str | None) -> bool:
    return normalize_role(role) == "admin"


def is_consultant(role: str | None) -> bool:
    return normalize_role(role) == "consultor"


def ensure_admin(role: str | None) -> None:
    if not is_admin(role):
        raise HTTPException(status_code=403, detail="No autorizado.")


def normalize_assignment_code(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


async def ensure_path_access(
    pool: AsyncConnectionPool,
    *,
    user_id: int | None,
    user_role: str | None,
    path: str,
) -> dict:
    normalized_r = normalize_role(user_role)
    if is_admin(normalized_r) or user_id is None:
        return await get_access_snapshot(pool, user_id=user_id or 0, role="admin")

    snapshot = await get_access_snapshot(
        pool,
        user_id=user_id,
        role=user_role,
    )
    allowed_paths = set(snapshot.get("allowedPaths", []))

    if path not in allowed_paths:
        raise HTTPException(status_code=403, detail="No autorizado.")

    return snapshot
