from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_billing_customers(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
) -> list[dict]:
    offset = (page - 1) * page_size
    query = """
        SELECT
            cs.supply_code,
            COALESCE(cs.customer_name, c.business_name, c.full_name, 'SUMINISTRO ' || cs.supply_code) AS customer_name,
            COALESCE(cs.district, c.district, 'SIN DISTRITO') AS district,
            c.payer_classification
        FROM public.customer_supplies cs
        JOIN public.customers c ON c.id = cs.customer_id
        WHERE EXISTS (
            SELECT 1
            FROM public.customer_debts cd
            WHERE cd.customer_supply_id = cs.id
            UNION ALL
            SELECT 1
            FROM public.customer_supply_billing_daily csbd
            WHERE csbd.supply_code = cs.supply_code
        )
        ORDER BY customer_name ASC
        LIMIT %s OFFSET %s
    """
    return await fetch_all_dict(pool, query, (page_size, offset))
