from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_data_sources_overview(pool: AsyncConnectionPool) -> list[dict]:
    query = """
        SELECT
            dataset_key,
            dataset_label,
            table_name,
            total_records,
            last_imported_at
        FROM (
            SELECT
                'contrastations'::text AS dataset_key,
                'Contrastaciones'::text AS dataset_label,
                'meter_contrastations'::text AS table_name,
                COUNT(*)::bigint AS total_records,
                MAX(imported_at) AS last_imported_at
            FROM public.meter_contrastations

            UNION ALL

            SELECT
                'meter-installations'::text AS dataset_key,
                'Instalacion de medidores'::text AS dataset_label,
                'meter_park_snapshots'::text AS table_name,
                COUNT(*)::bigint AS total_records,
                MAX(imported_at) AS last_imported_at
            FROM public.meter_park_snapshots

            UNION ALL

            SELECT
                'service-orders'::text AS dataset_key,
                'Ordenes de servicio'::text AS dataset_label,
                'commercial_inspections'::text AS table_name,
                COUNT(*)::bigint AS total_records,
                MAX(imported_at) AS last_imported_at
            FROM public.commercial_inspections
        ) sources
        ORDER BY dataset_label ASC
    """
    return await fetch_all_dict(pool, query)


async def fetch_contrastations(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    params: list = []
    conditions: list[str] = []

    if search:
        search_terms = [term.strip().lower() for term in search.split(" ") if term.strip()]
        for term in search_terms:
            params.append(f"%{term}%")
            conditions.append(
                """
                lower(concat_ws(' ',
                    supply_code,
                    meter_serial,
                    customer_name,
                    order_number::text,
                    result,
                    district,
                    contrast_lab
                )) like %s
                """
            )

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([page_size, offset])
    query = f"""
        SELECT
            supply_code,
            meter_serial,
            customer_name,
            order_number,
            result,
            district,
            contrast_lab,
            test_date::text AS test_date,
            imported_at::text AS imported_at
        FROM public.meter_contrastations
        {where_clause}
        ORDER BY test_date DESC NULLS LAST, order_number DESC NULLS LAST, supply_code ASC
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)


async def fetch_meter_installations(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    params: list = []
    conditions: list[str] = []

    if search:
        search_terms = [term.strip().lower() for term in search.split(" ") if term.strip()]
        for term in search_terms:
            params.append(f"%{term}%")
            conditions.append(
                """
                lower(concat_ws(' ',
                    supply_code,
                    meter_serial,
                    previous_meter_serial,
                    master_customer_name,
                    status_code,
                    office_code,
                    service_order_number::text
                )) like %s
                """
            )

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([page_size, offset])
    query = f"""
        SELECT
            supply_code,
            meter_serial,
            previous_meter_serial,
            master_customer_name,
            diameter_mm,
            installation_date::text AS installation_date,
            process_date::text AS process_date,
            status_code,
            office_code,
            service_order_number,
            imported_at::text AS imported_at
        FROM public.meter_park_snapshots
        {where_clause}
        ORDER BY installation_date DESC NULLS LAST, service_order_number DESC NULLS LAST, supply_code ASC
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)


async def fetch_service_orders(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    params: list = []
    conditions: list[str] = []

    if search:
        search_terms = [term.strip().lower() for term in search.split(" ") if term.strip()]
        for term in search_terms:
            params.append(f"%{term}%")
            conditions.append(
                """
                lower(concat_ws(' ',
                    supply_code,
                    customer_name,
                    meter_serial,
                    work_order_number,
                    inspection_typology,
                    district,
                    service_status
                )) like %s
                """
            )

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([page_size, offset])
    query = f"""
        SELECT
            supply_code,
            customer_name,
            meter_serial,
            work_order_number,
            inspection_typology,
            visit_date::text AS visit_date,
            district,
            service_status,
            office_code,
            imported_at::text AS imported_at
        FROM public.commercial_inspections
        {where_clause}
        ORDER BY visit_date DESC NULLS LAST, work_order_number DESC NULLS LAST, supply_code ASC
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)
