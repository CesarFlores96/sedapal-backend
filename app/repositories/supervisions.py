from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException

from app.authz import is_admin, normalize_role
from app.repositories.supervision_mapping import (
    FLAT_COLUMNS,
    VISIT_DATE_SQL,
    flat_columns_to_sections,
    section_to_flat_columns,
)


def build_inspection_snapshot(row: dict) -> dict:
    customer_document = row.get("dni") or row.get("ruc")
    return {
        "accessCode": row.get("co_accej"),
        "addressNumber": row.get("numero"),
        "addressStreet": row.get("calle"),
        "connectionDiameter": row.get("diam"),
        "customerCode": row.get("cus"),
        "customerDocument": customer_document,
        "customerName": row.get("nom_raz"),
        "customerPhone": row.get("telf_fax"),
        "district": row.get("distrito"),
        "employeeCode": row.get("cod_emp"),
        "emissionDate": row.get("fec_emis"),
        "generatedBy": row.get("gen_por"),
        "inspectionTypology": row.get("tip_os"),
        "itineraryCode": row.get("itin"),
        "meterSerial": row.get("medidor"),
        "observation": row.get("observac"),
        "officeCode": row.get("of_comer"),
        "propertyAccess": row.get("acceso_inmueb"),
        "propertyLocation": row.get("ubic_pred"),
        "resolutionDate": row.get("fe_res"),
        "routeCode": row.get("ruta"),
        "sourceCategory": None,
        "sourceYear": None,
        "supplyCode": str(row["nis_rad"]) if row.get("nis_rad") is not None else None,
        "supplyStatus": row.get("abast"),
        "supplyType": row.get("tipsum"),
        "urbanization": row.get("urbaniza"),
        "visitDate": row.get("visit_date"),
        "workOrderNumber": str(row["num_os"]) if row.get("num_os") is not None else None,
        "id": row.get("supervision_id"),
    }


def map_supervision_row(row: dict) -> dict:
    section4, section5 = flat_columns_to_sections(row)
    return {
        "completedAt": row.get("completed_at"),
        "createdAt": row.get("created_at"),
        "id": row.get("supervision_id"),
        "inspectionId": row.get("supervision_id"),
        "inspectionSnapshot": build_inspection_snapshot(row),
        "section4WaterConnection": section4,
        "section5SewerBox": section5,
        "status": row.get("status") or "draft",
        "updatedAt": row.get("updated_at"),
        "workOrderNumber": str(row["num_os"]) if row.get("num_os") is not None else None,
    }


# `SELECT *` no sirve aqui: necesitamos `visit_date` derivado de `fec_emis` (texto
# legado DD/MM/YYYY) y `customer_document` como fallback dni/ruc, igual que antes.
FIND_SUPERVISION_ROW_SQL = f"""
SELECT
  s.*,
  {VISIT_DATE_SQL}::text AS visit_date
FROM public.supervision s
WHERE s.num_os::text = %s
LIMIT 1;
"""


async def find_supervision_row(pool: AsyncConnectionPool, work_order_number: str) -> dict | None:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(FIND_SUPERVISION_ROW_SQL, [work_order_number])
            return await cursor.fetchone()


def ensure_order_access(row: dict | None, user_role: str | None, user_id: int | None) -> None:
    if not row:
        raise HTTPException(status_code=404, detail="No se encontro la orden de inspeccion.")

    normalized_role = normalize_role(user_role)
    if is_admin(normalized_role):
        return
    if user_id is None or int(row.get("assigned_user_id") or 0) != int(user_id):
        raise HTTPException(status_code=404, detail="No se encontro la orden de inspeccion.")


async def get_supervision_detail_or_throw(
    pool: AsyncConnectionPool,
    work_order_number: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    row = await find_supervision_row(pool, work_order_number)
    ensure_order_access(row, user_role, user_id)

    supervision = map_supervision_row(row)
    return {
        "inspectionOrder": supervision["inspectionSnapshot"],
        "pdfAvailable": supervision["status"] == "completed",
        "supervision": supervision,
    }


async def list_supervision_agenda(
    pool: AsyncConnectionPool,
    date: str | None,
    *,
    user_role: str | None,
    user_id: int | None,
) -> list[dict]:
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return []

    params: list = []
    conditions: list[str] = ["s.num_os IS NOT NULL"]

    if date:
        params.append(date)
        conditions.append(f"{VISIT_DATE_SQL} = %s::date")

    if not is_admin(normalized_role):
        params.append(user_id)
        conditions.append("s.assigned_user_id = %s")

    where_clause = " AND ".join(conditions)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT
                  s.supervision_id AS inspection_id,
                  s.num_os::text AS work_order_number,
                  {VISIT_DATE_SQL}::text AS visit_date,
                  s.nis_rad::text AS supply_code,
                  s.nom_raz AS customer_name,
                  s.distrito AS district,
                  s.tip_os AS inspection_typology,
                  NULL::text AS source_category,
                  s.status AS supervision_status
                FROM public.supervision s
                WHERE {where_clause}
                ORDER BY COALESCE(s.nom_raz, ''), s.num_os;
                """,
                params,
            )
            rows = await cursor.fetchall()
    return [
        {
            "customerName": row.get("customer_name"),
            "district": row.get("district"),
            "inspectionId": row.get("inspection_id"),
            "inspectionTypology": row.get("inspection_typology"),
            "sourceCategory": row.get("source_category"),
            "supplyCode": row.get("supply_code"),
            "supervisionStatus": row.get("supervision_status"),
            "visitDate": row.get("visit_date"),
            "workOrderNumber": row.get("work_order_number"),
        }
        for row in rows
    ]


async def get_supervision_detail(
    pool: AsyncConnectionPool,
    work_order_number: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict | None:
    row = await find_supervision_row(pool, work_order_number)
    if not row:
        return None
    ensure_order_access(row, user_role, user_id)
    supervision = map_supervision_row(row)
    return {
        "inspectionOrder": supervision["inspectionSnapshot"],
        "pdfAvailable": supervision["status"] == "completed",
        "supervision": supervision,
    }


async def ensure_supervision_draft(
    pool: AsyncConnectionPool,
    work_order_number: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    # La fila ya existe (viene del import legado); no hay nada que crear.
    # Se mantiene la funcion/endpoint por compatibilidad con clientes existentes.
    return await get_supervision_detail_or_throw(
        pool,
        work_order_number,
        user_role=user_role,
        user_id=user_id,
    )


async def save_supervision(
    pool: AsyncConnectionPool,
    work_order_number: str,
    payload: dict,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    row = await find_supervision_row(pool, work_order_number)
    ensure_order_access(row, user_role, user_id)

    section4 = payload.get("section4WaterConnection") or {}
    section5 = payload.get("section5SewerBox") or {}
    flat = section_to_flat_columns(section4, section5)

    assignments = ",\n          ".join(f"{column} = %s" for column in FLAT_COLUMNS)
    params = [flat[column] for column in FLAT_COLUMNS] + [work_order_number]

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                UPDATE public.supervision
                SET
                  {assignments},
                  updated_at = NOW()
                WHERE num_os::text = %s;
                """,
                params,
            )
        await connection.commit()
    return await get_supervision_detail_or_throw(
        pool,
        work_order_number,
        user_role=user_role,
        user_id=user_id,
    )


async def finalize_supervision(
    pool: AsyncConnectionPool,
    work_order_number: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    row = await find_supervision_row(pool, work_order_number)
    ensure_order_access(row, user_role, user_id)

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.supervision
                SET
                  status = 'completed',
                  completed_at = NOW(),
                  updated_at = NOW()
                WHERE num_os::text = %s;
                """,
                [work_order_number],
            )
        await connection.commit()
    return await get_supervision_detail_or_throw(
        pool,
        work_order_number,
        user_role=user_role,
        user_id=user_id,
    )
