from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_customers(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None,
) -> tuple[list[dict], int]:
    offset = (page - 1) * page_size
    filter_params: list[str] = []
    search_clause = ""
    if search:
        filter_params.extend([f"%{search.lower()}%", f"%{search.lower()}%"]) 
        search_clause = """
            WHERE lower(customer_name) LIKE %s
               OR lower(supply_search_text) LIKE %s
        """

    base_query = """
        WITH debt_by_customer AS (
            SELECT customer_id, SUM(total_soles) AS total_debt_soles
            FROM public.customer_debts
            WHERE status NOT IN ('pagada', 'condonada')
            GROUP BY customer_id
        ),
        customer_rows AS (
            SELECT
                c.id AS customer_id,
                COALESCE(MAX(cs.customer_code), c.id_doc_number, c.supply_code) AS customer_code,
                COALESCE(MAX(cs.customer_name), c.business_name, c.full_name) AS customer_name,
                COALESCE(MIN(cs.segment), 'Sin segmentar') AS affiliation,
                MIN(cs.supply_code) AS supply_code,
                COUNT(cs.id)::int AS supply_count,
                COALESCE(dbc.total_debt_soles, 0) AS total_debt_soles,
                c.payer_classification,
                COALESCE(c.phone_primary, c.phone_secondary, 'Sin celular') AS phone_mobile,
                string_agg(cs.supply_code, ' ') AS supply_search_text
            FROM public.customer_supplies cs
            JOIN public.customers c ON c.id = cs.customer_id
            LEFT JOIN debt_by_customer dbc ON dbc.customer_id = c.id
            GROUP BY c.id, c.id_doc_number, c.supply_code, c.business_name, c.full_name,
                c.payer_classification, c.phone_primary, c.phone_secondary, dbc.total_debt_soles
        )
    """
    data_query = f"""
        {base_query}
        SELECT customer_id, customer_code, customer_name, affiliation, supply_code,
            supply_count, total_debt_soles, payer_classification, phone_mobile
        FROM customer_rows
        {search_clause}
        ORDER BY total_debt_soles DESC, customer_name ASC NULLS LAST, customer_id ASC
        LIMIT %s OFFSET %s
    """
    count_query = f"""
        {base_query}
        SELECT COUNT(*)::int AS total
        FROM customer_rows
        {search_clause}
    """
    data = await fetch_all_dict(pool, data_query, [*filter_params, page_size, offset])
    count_rows = await fetch_all_dict(pool, count_query, filter_params)
    return data, (count_rows[0]["total"] if count_rows else 0)
