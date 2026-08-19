from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_chat_summary(pool: AsyncConnectionPool) -> dict[str, int | float]:
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
            (SELECT COUNT(*) FROM public.customers) AS customers,
            (SELECT COUNT(*) FROM public.customer_supplies) AS supplies,
            (SELECT COUNT(*) FROM public.customer_debts) AS debts,
            (
                SELECT COALESCE(SUM(total_soles), 0)
                FROM public.customer_debts
                WHERE status NOT IN ('pagada', 'condonada')
            ) AS pending_debt;
        """,
    )
    row = rows[0] if rows else {}
    return {
        "customers": int(row.get("customers", 0) or 0),
        "supplies": int(row.get("supplies", 0) or 0),
        "debts": int(row.get("debts", 0) or 0),
        "pendingDebt": float(row.get("pending_debt", 0) or 0),
    }


async def fetch_chat_schema(pool: AsyncConnectionPool) -> dict[str, list[dict[str, str]]]:
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
          table_name,
          column_name,
          data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name NOT IN ('spatial_ref_sys', 'geography_columns', 'geometry_columns')
        ORDER BY table_name, ordinal_position;
        """,
    )

    schema: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        table_name = str(row.get("table_name") or "")
        if not table_name:
            continue
        schema.setdefault(table_name, []).append(
            {
                "column": str(row.get("column_name") or ""),
                "type": str(row.get("data_type") or ""),
            }
        )

    return schema
