from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_facturacion(
    pool: AsyncConnectionPool,
    suministro: str,
    page: int | None = None,
    page_size: int | None = None,
) -> list[dict]:
    query = """
        WITH debt_ranked AS (
            SELECT
                cd.commercial_office_code,
                cd.period_year::int AS period_year,
                cd.period_month::int AS period_month,
                cd.issue_date,
                cd.reading_date,
                cd.concept,
                cd.amount_soles,
                cd.billed_volume_m3,
                'customer_debts'::text AS source_name,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        cd.supply_code,
                        cd.period_year::int,
                        cd.period_month::int,
                        cd.concept
                    ORDER BY
                        cd.updated_at DESC NULLS LAST,
                        cd.created_at DESC NULLS LAST,
                        cd.id DESC
                ) AS source_rank
            FROM public.customer_debts cd
            WHERE cd.supply_code = %s
        ),
        daily_ranked AS (
            SELECT
                csbd.office_code AS commercial_office_code,
                EXTRACT(YEAR FROM csbd.issue_date)::int AS period_year,
                EXTRACT(MONTH FROM csbd.issue_date)::int AS period_month,
                csbd.issue_date,
                csbd.reading_date,
                csbd.concept,
                csbd.amount_soles,
                csbd.billed_volume_m3,
                'customer_supply_billing_daily'::text AS source_name,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        csbd.supply_code,
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
        ),
        canonical_billing AS (
            SELECT
                debt.commercial_office_code,
                debt.period_year,
                debt.period_month,
                debt.issue_date,
                debt.reading_date,
                debt.concept,
                debt.amount_soles,
                debt.billed_volume_m3,
                debt.source_name
            FROM debt_ranked debt
            WHERE debt.source_rank = 1

            UNION ALL

            SELECT
                daily.commercial_office_code,
                daily.period_year,
                daily.period_month,
                daily.issue_date,
                daily.reading_date,
                daily.concept,
                daily.amount_soles,
                daily.billed_volume_m3,
                daily.source_name
            FROM daily_ranked daily
            WHERE daily.source_rank = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM debt_ranked debt
                  WHERE debt.source_rank = 1
                    AND debt.period_year = daily.period_year
                    AND debt.period_month = daily.period_month
                    AND debt.concept = daily.concept
              )
        )
        SELECT
            billing.commercial_office_code,
            billing.period_year,
            billing.period_month,
            billing.issue_date,
            billing.reading_date,
            billing.concept,
            billing.amount_soles,
            billing.billed_volume_m3,
            billing.source_name
        FROM canonical_billing AS billing
        ORDER BY billing.period_year DESC, billing.period_month DESC, billing.concept ASC
    """
    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        query += " LIMIT %s OFFSET %s"
        return await fetch_all_dict(pool, query, (suministro, suministro, page_size, offset))
    else:
        return await fetch_all_dict(pool, query, (suministro, suministro))
