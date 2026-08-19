from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict, execute_statement


# Ventana de aviso "por vencer": 1 mes antes de la fecha de vencimiento.
# customer_supplies es la fuente real de datos operativos de medidores en esta
# BD local; meters/meter_types siguen vacias. El vencimiento se calcula SOLO sobre
# el último medidor instalado (más reciente por anio_fabric) de cada suministro,
# obtenido de meter_registry. El diametro se toma de customer_supplies (que tiene
# el mapeo decodificado); la fecha de instalacion se toma del meter_registry.
METER_PLANNING_CTE_SQL = """
  latest_meter AS (
    SELECT DISTINCT ON (nis_rad)
      nis_rad,
      num_medidor,
      (anio_fabric::timestamp)::date AS installation_date
    FROM public.meter_registry
    WHERE registry_status = 'instalado' AND nis_rad IS NOT NULL
    ORDER BY nis_rad, (anio_fabric::timestamp)::date DESC NULLS LAST, num_medidor DESC
  ),
  meter_planning AS (
    SELECT
      cs.supply_code,
      coalesce(cs.customer_name, c.business_name, c.full_name) AS master_customer_name,
      COALESCE(lm.num_medidor, cs.meter_code) AS meter_serial,
      NULLIF(cs.meter_diameter, '')::int AS diameter_mm,
      COALESCE(lm.installation_date, cs.meter_installation_date)::text AS installation_date,
      cs.meter_status,
      cs.supply_status,
      cs.segment AS segment_name,
      cs.office_name,
      cs.zone_name,
      dc.vida_util_anios,
      (COALESCE(lm.installation_date, cs.meter_installation_date) + make_interval(years => dc.vida_util_anios::int))::date::text AS due_date,
      CASE
        WHEN COALESCE(lm.installation_date, cs.meter_installation_date) IS NULL OR dc.vida_util_anios IS NULL THEN 'sin_datos'
        WHEN (COALESCE(lm.installation_date, cs.meter_installation_date) + make_interval(years => dc.vida_util_anios::int)) < CURRENT_DATE THEN 'vencido'
        WHEN (COALESCE(lm.installation_date, cs.meter_installation_date) + make_interval(years => dc.vida_util_anios::int)) < (CURRENT_DATE + interval '1 month') THEN 'por_vencer'
        ELSE 'vigente'
      END AS vencimiento_status
    FROM public.customer_supplies cs
    JOIN public.customers c ON c.id = cs.customer_id
    LEFT JOIN latest_meter lm ON lm.nis_rad = cs.supply_code
    LEFT JOIN public.diameter_catalog dc ON dc.diameter_mm = NULLIF(cs.meter_diameter, '')::int
  )
"""


async def fetch_meters(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
    status: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    conditions: list[str] = []
    params: list = []

    if search:
        conditions.append(
            "lower(concat_ws(' ', supply_code, master_customer_name, meter_serial, office_name, "
            "segment_name, supply_status)) LIKE %s"
        )
        params.append(f"%{search.strip().lower()}%")

    if status:
        conditions.append("vencimiento_status = %s")
        params.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([page_size, offset])

    query = f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT
          supply_code,
          master_customer_name,
          meter_serial,
          diameter_mm,
          installation_date,
          meter_status,
          supply_status,
          segment_name,
          office_name,
          zone_name,
          vida_util_anios,
          due_date,
          vencimiento_status
        FROM meter_planning
        {where_clause}
        ORDER BY
          CASE vencimiento_status WHEN 'vencido' THEN 0 WHEN 'por_vencer' THEN 1 WHEN 'vigente' THEN 2 ELSE 3 END,
          supply_code
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)


async def fetch_meter_by_supply_code(pool: AsyncConnectionPool, supply_code: str) -> list[dict]:
    query = f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT
          supply_code,
          master_customer_name,
          meter_serial,
          diameter_mm,
          installation_date,
          meter_status,
          supply_status,
          segment_name,
          office_name,
          zone_name,
          vida_util_anios,
          due_date,
          vencimiento_status
        FROM meter_planning
        WHERE supply_code = %s
        LIMIT 100
    """
    return await fetch_all_dict(pool, query, [supply_code])


async def fetch_meter_vencimiento_rows(pool: AsyncConnectionPool, status: str) -> list[dict]:
    """Lista completa (sin paginar) de medidores en un estado de vencimiento,
    usada por el correo semanal de alerta. vencido/por_vencer son subconjuntos
    chicos, no se espera que crezcan a un tamano que requiera paginacion."""
    query = f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT supply_code, master_customer_name, meter_serial, office_name, installation_date, due_date
        FROM meter_planning
        WHERE vencimiento_status = %s
        ORDER BY due_date ASC NULLS LAST
    """
    return await fetch_all_dict(pool, query, [status])


async def fetch_meter_planning_stats(pool: AsyncConnectionPool) -> dict:
    counts_rows = await fetch_all_dict(
        pool,
        f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT vencimiento_status, COUNT(*)::int AS total
        FROM meter_planning
        GROUP BY vencimiento_status
        """,
    )
    by_office = await fetch_all_dict(
        pool,
        f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT office_name, vencimiento_status, COUNT(*)::int AS total
        FROM meter_planning
        WHERE vencimiento_status IN ('vencido', 'por_vencer')
        GROUP BY office_name, vencimiento_status
        ORDER BY total DESC
        """,
    )
    by_diameter = await fetch_all_dict(
        pool,
        f"""
        WITH {METER_PLANNING_CTE_SQL}
        SELECT diameter_mm, COUNT(*)::int AS total
        FROM meter_planning
        WHERE vencimiento_status IN ('vencido', 'por_vencer')
        GROUP BY diameter_mm
        ORDER BY diameter_mm ASC NULLS LAST
        """,
    )

    counts = {"vencido": 0, "por_vencer": 0, "vigente": 0, "sin_datos": 0}
    for row in counts_rows:
        counts[row["vencimiento_status"]] = row["total"]

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "byOffice": by_office,
        "byDiameter": by_diameter,
    }


async def update_meter(
    pool: AsyncConnectionPool,
    supply_code: str,
    meter_serial: str | None,
    diameter_mm: int | None,
    installation_date: str | None,
    supply_status: str | None,
    segment_name: str | None,
    meter_status: str | None,
) -> None:
    # supply_status y segment son NOT NULL en customer_supplies: usar COALESCE
    # para que un campo ausente/None en el PATCH no borre el valor existente.
    await execute_statement(
        pool,
        """
        UPDATE public.customer_supplies
        SET
          meter_code = %s,
          meter_diameter = %s,
          meter_installation_date = %s,
          supply_status = coalesce(%s, supply_status),
          segment = coalesce(%s, segment),
          meter_status = %s,
          updated_at = now()
        WHERE supply_code = %s
        """,
        [
            meter_serial,
            str(diameter_mm) if diameter_mm is not None else None,
            installation_date,
            supply_status,
            segment_name,
            meter_status,
            supply_code,
        ],
    )


async def fetch_meter_registry(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    registry_status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    conditions: list[str] = []
    params: list = []

    if registry_status:
        conditions.append("registry_status = %s")
        params.append(registry_status)

    if search:
        conditions.append(
            "lower(concat_ws(' ', num_medidor, nis_rad, codigo_mca, cod_diametro)) LIKE %s"
        )
        params.append(f"%{search.strip().lower()}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([page_size, offset])

    query = f"""
        SELECT
          num_medidor,
          nis_rad,
          registry_status,
          est_act,
          codigo_mca,
          cod_diametro,
          cod_unicom,
          cod_alma,
          anio_fabric::text,
          tip_lectura,
          cod_contratista,
          source_file,
          imported_at
        FROM public.meter_registry
        {where_clause}
        ORDER BY num_medidor
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)


async def fetch_meter_registry_stats(pool: AsyncConnectionPool) -> list[dict]:
    return await fetch_all_dict(
        pool,
        """
        SELECT registry_status, COUNT(*)::int AS total
        FROM public.meter_registry
        GROUP BY registry_status
        ORDER BY total DESC
        """,
    )
