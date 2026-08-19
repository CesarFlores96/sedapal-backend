from fastapi import APIRouter, HTTPException, status

from app.database import get_pool


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/db")
async def check_database_health() -> dict[str, object]:
    pool = get_pool()
    try:
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1;")
                row = await cursor.fetchone()
    except Exception as exc:  # pragma: no cover - runtime safeguard
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No fue posible conectar con PostgreSQL: {exc}",
        ) from exc

    return {"success": True, "database": "ok", "result": row[0] if row else None}
