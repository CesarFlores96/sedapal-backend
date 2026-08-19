from datetime import date, datetime

from fastapi import HTTPException
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from app.authz import is_admin, normalize_role

PLANILLA_ROW_COLUMNS = (
    "id, fecha, nis, ruta, itin, aol, medidor, direccion, distrito, cliente, ciclo, "
    "lectura, supervisor, area_solicitante, carga, observaciones, assigned_user_id, "
    "status, completed_at, derivation_sent_at, source_file_name, uploaded_by_user_id, "
    "uploaded_by_name, created_at"
)


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


async def get_planilla_row(
    pool: AsyncConnectionPool,
    planilla_id: int,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    await get_planilla_record_access(pool, planilla_id, user_role=user_role, user_id=user_id)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {PLANILLA_ROW_COLUMNS} FROM public.planillas WHERE id = %s LIMIT 1;",
                (planilla_id,),
            )
            row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")
    return row


async def mark_planilla_derivation_sent(
    pool: AsyncConnectionPool,
    planilla_id: int,
) -> None:
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "UPDATE public.planillas SET derivation_sent_at = NOW() WHERE id = %s;",
                (planilla_id,),
            )
        await connection.commit()


async def list_planillas_by_day(
    pool: AsyncConnectionPool,
    fecha_iso: str | None,
    *,
    user_role: str | None,
    user_id: int | None,
) -> list[dict]:
    """`fecha_iso` es opcional: cuando se omite devuelve todas las planillas
    visibles para el actor (usado por movil, que lista todas las fechas de una
    sola vez y agrupa por dia en el cliente, igual que antes hacia contra
    Supabase directo sin filtro de fecha). El caller web (`listPlanillasByDay`)
    siempre manda `fecha`, asi que su comportamiento no cambia."""
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return []

    conditions: list[str] = []
    params: list = []
    if fecha_iso:
        conditions.append("fecha = %s")
        params.append(fecha_iso)
    if not is_admin(normalized_role):
        conditions.append("assigned_user_id = %s")
        params.append(user_id)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT {PLANILLA_ROW_COLUMNS}
                FROM public.planillas
                {where_clause}
                ORDER BY fecha DESC, id ASC;
                """,
                params,
            )
            return await cursor.fetchall()


async def update_planilla_observaciones(
    pool: AsyncConnectionPool,
    planilla_id: int,
    observaciones: str | None = None,
    *,
    lectura: str | None = None,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    """Actualiza `observaciones` y/o `lectura` de una planilla. Ambos son
    opcionales e independientes (el movil autoguarda cada campo por separado
    segun lo que el usuario haya tocado), igual que el `update` parcial que
    antes armaba `savePlanilla` en apps/mobile/src/api/client.ts contra
    Supabase directo."""
    await get_planilla_record_access(pool, planilla_id, user_role=user_role, user_id=user_id)

    set_clauses = ["updated_at = NOW()"]
    params: list = []
    if observaciones is not None:
        set_clauses.append("observaciones = %s")
        params.append(observaciones)
    if lectura is not None:
        set_clauses.append("lectura = %s")
        params.append(lectura)
    params.append(planilla_id)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                UPDATE public.planillas
                SET {", ".join(set_clauses)}
                WHERE id = %s
                RETURNING id, observaciones, lectura;
                """,
                params,
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")
    return row


async def assign_planilla_user(
    pool: AsyncConnectionPool,
    planilla_id: int,
    target_user_id: int,
    *,
    user_role: str | None,
) -> dict:
    if not is_admin(normalize_role(user_role)):
        raise HTTPException(status_code=403, detail="Solo un administrador puede reasignar una planilla.")

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE public.planillas
                SET assigned_user_id = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id, assigned_user_id;
                """,
                (target_user_id, planilla_id),
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")
    return row


async def complete_planilla(
    pool: AsyncConnectionPool,
    planilla_id: int,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    access = await get_planilla_record_access(pool, planilla_id, user_role=user_role, user_id=user_id)
    if access.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Esta fila ya fue completada.")

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE public.planillas
                SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                WHERE id = %s
                RETURNING id, status, completed_at;
                """,
                (planilla_id,),
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")
    return row


async def insert_planilla_rows(
    pool: AsyncConnectionPool,
    rows: list[dict],
) -> list[dict]:
    """Insert batcheado de filas extraidas de un PDF de planilla (accion de admin,
    verificada en el router). Nunca un INSERT por fila."""
    if not rows:
        return []

    columns = [
        "fecha", "nis", "ruta", "itin", "aol", "medidor", "direccion", "distrito",
        "cliente", "ciclo", "supervisor", "area_solicitante", "carga",
        "assigned_user_id", "source_file_name", "uploaded_by_user_id", "uploaded_by_name",
    ]
    placeholders = ", ".join(f"%({column})s" for column in columns)
    column_list = ", ".join(columns)

    inserted: list[dict] = []
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            for row in rows:
                params = {column: row.get(column) for column in columns}
                await cursor.execute(
                    f"""
                    INSERT INTO public.planillas ({column_list})
                    VALUES ({placeholders})
                    RETURNING {PLANILLA_ROW_COLUMNS};
                    """,
                    params,
                )
                inserted.append(await cursor.fetchone())
        await connection.commit()

    return inserted
