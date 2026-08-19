from psycopg_pool import AsyncConnectionPool

from app.sedapalgis.repositories.shared import fetch_all

# Misma composición canónica que gis.py/reportes.py: customer_debts es la
# fuente autoritativa por periodo/concepto; customer_supply_billing_daily solo
# rellena huecos que la primera no cubre todavía.
FETCH_FACTURACION_SQL = """
    WITH debt_ranked AS (
        SELECT
            cd.period_year::int AS period_year,
            cd.period_month::int AS period_month,
            cd.concept,
            cd.billed_volume_m3::float8 AS billed_volume_m3,
            cd.amount_soles::float8 AS amount_soles,
            ROW_NUMBER() OVER (
                PARTITION BY cd.period_year::int, cd.period_month::int, cd.concept
                ORDER BY cd.updated_at DESC NULLS LAST, cd.created_at DESC NULLS LAST, cd.id DESC
            ) AS source_rank
        FROM public.customer_debts cd
        WHERE cd.supply_code = %s
    ),
    daily_ranked AS (
        SELECT
            EXTRACT(YEAR FROM csbd.issue_date)::int AS period_year,
            EXTRACT(MONTH FROM csbd.issue_date)::int AS period_month,
            csbd.concept,
            csbd.billed_volume_m3::float8 AS billed_volume_m3,
            csbd.amount_soles::float8 AS amount_soles,
            ROW_NUMBER() OVER (
                PARTITION BY
                    EXTRACT(YEAR FROM csbd.issue_date)::int,
                    EXTRACT(MONTH FROM csbd.issue_date)::int,
                    csbd.concept
                ORDER BY
                    csbd.source_batch_date DESC NULLS LAST,
                    csbd.imported_at DESC NULLS LAST,
                    csbd.source_file DESC NULLS LAST,
                    csbd.source_line_number DESC NULLS LAST,
                    csbd.id DESC
            ) AS source_rank
        FROM public.customer_supply_billing_daily csbd
        WHERE csbd.supply_code = %s
          AND csbd.issue_date IS NOT NULL
    )
    SELECT period_year, period_month, concept, billed_volume_m3, amount_soles
    FROM debt_ranked
    WHERE source_rank = 1

    UNION ALL

    SELECT daily.period_year, daily.period_month, daily.concept, daily.billed_volume_m3, daily.amount_soles
    FROM daily_ranked daily
    WHERE daily.source_rank = 1
      AND NOT EXISTS (
        SELECT 1 FROM debt_ranked debt
        WHERE debt.source_rank = 1
          AND debt.period_year = daily.period_year
          AND debt.period_month = daily.period_month
          AND debt.concept = daily.concept
      )
"""


async def fetch_facturacion(pool: AsyncConnectionPool, supply_code: str) -> list[dict]:
    return await fetch_all(pool, FETCH_FACTURACION_SQL, [supply_code, supply_code])
