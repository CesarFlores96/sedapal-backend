from fastapi import HTTPException
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from app.authz import is_admin, normalize_role


async def get_planilla_record_access(
    pool: AsyncConnectionPool,
    planilla_id: int,
    *,
    user_role: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Verifica si la planilla existe y si el usuario tiene acceso a ella."""
    normalized_role = normalize_role(user_role)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT
                    id AS supervision_id,
                    assigned_user_id,
                    'planilla-' || id AS num_os,
                    nis AS nis_rad,
                    fecha,
                    created_at
                FROM public.planillas
                WHERE id = %s
                LIMIT 1;
                """,
                (planilla_id,),
            )
            row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")

    if not is_admin(normalized_role) and (
        user_id is None or row.get("assigned_user_id") != user_id
    ):
        raise HTTPException(
            status_code=403,
            detail="No tienes permisos para modificar evidencias de esta planilla.",
        )

    return row
