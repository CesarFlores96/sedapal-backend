from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_inspections(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    supply_code: str | None = None,
    search: str | None = None,
) -> list[dict]:
    offset = (page - 1) * page_size
    params: list = []
    conditions: list[str] = []
    
    if supply_code:
        params.append(supply_code)
        conditions.append("supply_code = %s")
        
    if search:
        search_terms = [term.strip().lower() for term in search.split(" ") if term.strip()]
        for term in search_terms:
            params.append(f"%{term}%")
            conditions.append(
                "lower(concat_ws(' ', supply_code, customer_name, meter_serial, work_order_number, inspection_typology, district)) like %s"
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
            visit_date::text,
            district,
            office_code,
            service_status,
            service_detail,
            box_location,
            box_state,
            lid_state,
            box_cover_security,
            clandestine_status,
            property_status,
            connection_type,
            inspection_result,
            no_entry_reason,
            source_category,
            is_fuente_propia
        FROM public.commercial_inspections
        {where_clause}
        ORDER BY visit_date DESC NULLS LAST, work_order_number ASC
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)
