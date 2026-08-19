"""Proxy generico de lectura/escritura sobre `public.supervision`, pensado para
reemplazar el acceso directo a Supabase que hoy hace
`apps/web/src/modules/supervisiones/lib/supabase-supervision.ts`. Esa capa web
ya tiene toda la logica de mapeo (section4/section5/generalFields, TXT legado,
etc.) y solo necesita un backend generico tipo Postgres para leer/escribir
filas completas por num_os o supervision_id -- no reimplementa esa logica en
Python, solo expone las mismas operaciones que antes hacia el cliente
supabase-js (select/update/insert con RETURNING *), con la misma autorizacion
por `assigned_user_id` que ya aplica el resto del backend.
"""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException

from app.authz import is_admin, normalize_role

SUMMARY_COLUMNS = (
    "supervision_id, num_os, nis_rad, nom_raz, tip_os, fec_emis, observac, of_comer, "
    "source_file_name, created_at, updated_at, status, assigned_user_id, completed_at, "
    "gen_por, cus, corr_cup, ruta, itin, calle, distrito, medidor, cod_abas, cua, "
    "deleted_at, deleted_by_name, exported_at, derivation_sent_at"
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _ensure_access(row: dict | None, *, user_role: str | None, user_id: int | None) -> dict:
    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de supervision.")
    if not is_admin(normalize_role(user_role)) and (
        user_id is None or row.get("assigned_user_id") != user_id
    ):
        raise HTTPException(status_code=403, detail="No autorizado.")
    return row


async def get_supervision_raw(
    pool: AsyncConnectionPool,
    *,
    num_os: str | None = None,
    supervision_id: int | None = None,
    include_deleted: bool = False,
) -> dict | None:
    if num_os is None and supervision_id is None:
        raise HTTPException(status_code=400, detail="Falta num_os o supervisionId.")

    condition = "num_os::text = %s" if num_os is not None else "supervision_id = %s"
    param = num_os if num_os is not None else supervision_id
    delete_clause = "" if include_deleted else "AND deleted_at IS NULL"

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT * FROM public.supervision WHERE {condition} {delete_clause} LIMIT 1;",
                (param,),
            )
            return await cursor.fetchone()


async def update_supervision_raw(
    pool: AsyncConnectionPool,
    updates: dict,
    *,
    num_os: str | None = None,
    supervision_id: int | None = None,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    if num_os is None and supervision_id is None:
        raise HTTPException(status_code=400, detail="Falta num_os o supervisionId.")

    condition = "num_os::text = %s" if num_os is not None else "supervision_id = %s"
    param = num_os if num_os is not None else supervision_id

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT * FROM public.supervision WHERE {condition} AND deleted_at IS NULL LIMIT 1;",
                (param,),
            )
            existing = await cursor.fetchone()
            _ensure_access(existing, user_role=user_role, user_id=user_id)

            if not updates:
                return existing

            assignments = ", ".join(f"{_quote(column)} = %s" for column in updates)
            params = list(updates.values()) + [param]
            await cursor.execute(
                f"""
                UPDATE public.supervision
                SET {assignments}
                WHERE {condition}
                RETURNING *;
                """,
                params,
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de supervision.")
    return row


async def insert_supervision_raw(
    pool: AsyncConnectionPool,
    payload: dict,
) -> dict:
    columns = list(payload.keys())
    column_list = ", ".join(_quote(column) for column in columns)
    placeholders = ", ".join(f"%({column})s" for column in columns)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                INSERT INTO public.supervision ({column_list})
                VALUES ({placeholders})
                RETURNING *;
                """,
                payload,
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(status_code=500, detail="No se pudo crear la supervision.")
    return row


async def list_supervision_raw(
    pool: AsyncConnectionPool,
    *,
    fec_emis_ddmmyyyy: str | None,
    deleted: bool,
    user_role: str | None,
    user_id: int | None,
    limit: int = 1000,
) -> list[dict]:
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return []

    conditions = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]
    params: list = []

    if fec_emis_ddmmyyyy:
        conditions.append("fec_emis = %s")
        params.append(fec_emis_ddmmyyyy)

    if not is_admin(normalized_role):
        conditions.append("assigned_user_id = %s")
        params.append(user_id)

    params.append(limit)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT {SUMMARY_COLUMNS}
                FROM public.supervision
                WHERE {" AND ".join(conditions)}
                ORDER BY updated_at DESC NULLS LAST, num_os DESC NULLS LAST
                LIMIT %s;
                """,
                params,
            )
            return await cursor.fetchall()


async def list_supervision_raw_by_supply(
    pool: AsyncConnectionPool,
    supply_code: str,
    *,
    user_role: str | None,
    user_id: int | None,
    limit: int = 100,
) -> list[dict]:
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return []

    conditions = ["nis_rad = %s", "deleted_at IS NULL"]
    params: list = [supply_code]
    if not is_admin(normalized_role):
        conditions.append("assigned_user_id = %s")
        params.append(user_id)
    params.append(min(max(limit, 1), 100))

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT {SUMMARY_COLUMNS}
                FROM public.supervision
                WHERE {" AND ".join(conditions)}
                ORDER BY updated_at DESC NULLS LAST, supervision_id DESC
                LIMIT %s;
                """,
                params,
            )
            return await cursor.fetchall()


REPORT_COLUMNS = (
    "supervision_id, num_os, nis_rad, tip_os, fec_vis, fec_emis, observm1, observm1_ext"
)


async def list_supervision_raw_activity_report(
    pool: AsyncConnectionPool,
    target_user_id: int,
    month_suffix: str,
) -> list[dict]:
    """month_suffix es el patron '/MM/YYYY' para filtrar fec_vis/fec_emis (texto
    DD/MM/YYYY) por mes -- misma logica que el `.or(...)` que hacia supabase-js."""
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT {REPORT_COLUMNS}
                FROM public.supervision
                WHERE assigned_user_id = %s
                  AND status = 'completed'
                  AND deleted_at IS NULL
                  AND (fec_vis LIKE %s OR fec_emis LIKE %s);
                """,
                (target_user_id, f"%{month_suffix}", f"%{month_suffix}"),
            )
            return await cursor.fetchall()


async def list_supervision_raw_completed_range(
    pool: AsyncConnectionPool,
    start: str,
    end: str,
    *,
    only_unexported: bool,
    require_num_os: bool,
    user_role: str | None,
    user_id: int | None,
) -> list[dict]:
    normalized_role = normalize_role(user_role)
    conditions = [
        "status = 'completed'",
        "deleted_at IS NULL",
        "completed_at >= %s::timestamptz",
        "completed_at <= %s::timestamptz",
    ]
    params: list = [f"{start}T00:00:00", f"{end}T23:59:59"]

    if only_unexported:
        conditions.append("exported_at IS NULL")
    if require_num_os:
        conditions.append("num_os IS NOT NULL")
    if not is_admin(normalized_role):
        conditions.append("assigned_user_id = %s")
        params.append(user_id)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT * FROM public.supervision
                WHERE {" AND ".join(conditions)}
                ORDER BY completed_at ASC;
                """,
                params,
            )
            return await cursor.fetchall()
