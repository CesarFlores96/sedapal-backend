from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_readings(
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
        conditions.append("readings.supply_code = %s")
        
    if search:
        search_terms = [term.strip().lower() for term in search.split(" ") if term.strip()]
        for term in search_terms:
            params.append(f"%{term}%")
            conditions.append(
                "lower(concat_ws(' ', readings.supply_code, readings.customer_name, readings.meter_serial, readings.reading_observation)) like %s"
            )

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    params.extend([page_size, offset])
    query = f"""
        SELECT
            readings.office_code,
            readings.reading_year,
            readings.reading_month,
            readings.cycle_code,
            readings.route_code,
            readings.itinerary_code,
            readings.aol_code,
            readings.customer_name,
            readings.meter_serial,
            readings.previous_reading,
            readings.tariff_label,
            readings.supply_code,
            readings.cua_code,
            readings.alternate_meter_serial,
            readings.reading_observation,
            readings.current_reading,
            readings.incidence_code_1,
            readings.incidence_detail_1,
            readings.incidence_code_2,
            readings.incidence_detail_2,
            readings.reading_date,
            readings.reading_time,
            readings.reader_employee_code,
            readings.source_file,
            readings.source_office_code,
            state_reading.customer_name AS state_customer_name,
            state_reading.reading_date AS state_reading_date,
            state_reading.reading_type AS state_reading_type,
            state_reading.meter_type AS state_meter_type,
            state_reading.diameter_mm AS state_diameter,
            state_reading.reading_value AS state_reading_value,
            state_reading.incidence_label AS state_incidence,
            state_reading.incidence_detail AS state_detail,
            state_reading.observation AS state_observation,
            state_reading.code_id AS state_code_id
        FROM public.customer_supply_readings readings
        LEFT JOIN LATERAL (
            SELECT
                state.customer_name,
                state.reading_date,
                state.reading_type,
                state.meter_type,
                state.diameter_mm,
                state.reading_value,
                state.incidence_label,
                state.incidence_detail,
                state.observation,
                state.code_id
            FROM public.customer_supply_state_readings state
            WHERE state.supply_code = readings.supply_code
            ORDER BY
                CASE
                    WHEN state.meter_serial IS NOT NULL AND state.meter_serial = readings.meter_serial THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN state.reading_date IS NOT NULL AND state.reading_date = readings.reading_date THEN 0
                    WHEN state.reading_year = readings.reading_year AND state.reading_month = readings.reading_month THEN 1
                    ELSE 2
                END,
                state.reading_date DESC NULLS LAST
            LIMIT 1
        ) state_reading ON TRUE
        {where_clause}
        ORDER BY readings.reading_date DESC NULLS LAST, readings.reading_time DESC NULLS LAST
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, params)
