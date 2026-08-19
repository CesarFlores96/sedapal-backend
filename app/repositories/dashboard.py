import asyncio
import time
from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


def map_office_name(value: str | None) -> str:
    code = (value or "").strip()
    if code == "1001":
        return "GRANDES CLIENTES"
    if code == "5111":
        return "FUENTE PROPIA"
    return code or "SIN OFICINA"


# In-memory cache: dashboard data changes only with nightly imports/syncs, not
# per-request, so a short TTL avoids re-running ~15 heavy aggregate queries on
# every dashboard load/tab switch.
_CACHE_TTL_SECONDS = 60
_cache: dict[str, tuple[float, dict]] = {}


async def fetch_dashboard_data(pool: AsyncConnectionPool, tab: str | None = None) -> dict:
    cache_key = tab or "all"
    cached = _cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    data = await _query_dashboard_data(pool, tab)
    _cache[cache_key] = (time.monotonic(), data)
    return data


async def _query_dashboard_data(pool: AsyncConnectionPool, tab: str | None = None) -> dict:
    async def fetch_cond(cond: bool, pool: AsyncConnectionPool, sql: str):
        if cond:
            return await fetch_all_dict(pool, sql)
        return []

    (
        customers,
        monthly_payments,
        offices,
        tariffs,
        top_payers,
        totals,
        top_debtors,
        debt_totals,
        billed_volume_projection,
        billed_amount_projection,
        debt_age_ranges,
        debt_by_office,
        top_uses_by_debt,
        debt_by_tariff,
        debt_by_zone,
    ) = await asyncio.gather(
        fetch_cond(not tab or tab == "resumen",
            pool,
            """
            WITH debt_by_supply AS (
              SELECT customer_supply_id, SUM(total_soles) AS supply_debt_soles
              FROM public.customer_debts
              WHERE status NOT IN ('pagada', 'condonada')
              GROUP BY customer_supply_id
            ),
            payments_by_supply AS (
              SELECT supply_code, MAX(payment_date) AS last_payment_date
              FROM public.customer_payments
              GROUP BY supply_code
            )
            SELECT
              c.id AS customer_id,
              COALESCE(cs.customer_code, c.id_doc_number, c.supply_code) AS customer_code,
              COALESCE(cs.customer_name, c.business_name, c.full_name) AS customer_name,
              COALESCE(cs.district, c.district, 'SIN DISTRITO') AS district,
              c.payer_classification,
              COALESCE(c.phone_primary, c.phone_secondary, 'Sin celular') AS phone_mobile,
              COALESCE(cs.segment, 'Sin segmentar') AS segment_name,
              cs.supply_code,
              COALESCE(dbs.supply_debt_soles, 0) AS supply_debt_soles,
              COALESCE(pbs.last_payment_date, c.last_payment_date) AS last_payment_date
            FROM public.customer_supplies cs
            JOIN public.customers c ON c.id = cs.customer_id
            LEFT JOIN debt_by_supply dbs ON dbs.customer_supply_id = cs.id
            LEFT JOIN payments_by_supply pbs ON pbs.supply_code = cs.supply_code
            ORDER BY cs.supply_code;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            SELECT amount_soles, payment_date
            FROM public.customer_payments
            WHERE payment_date IS NOT NULL
            ORDER BY payment_date ASC;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            SELECT
              office_code,
              COUNT(*)::int AS payment_count,
              COALESCE(SUM(amount_soles), 0) AS total_amount
            FROM public.customer_payments
            GROUP BY office_code
            ORDER BY total_amount DESC, office_code ASC;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            SELECT
              tariff_code,
              COUNT(*)::int AS payment_count,
              COALESCE(SUM(amount_soles), 0) AS total_amount
            FROM public.customer_payments
            GROUP BY tariff_code
            ORDER BY total_amount DESC, tariff_code ASC;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            SELECT
              COALESCE(source_customer_name, 'SIN NOMBRE') AS customer_name,
              COALESCE(MAX(office_code), 'SIN OFICINA') AS office_code,
              COALESCE(MAX(CASE
                WHEN office_code = '5111' THEN 'FUENTE PROPIA'
                WHEN office_code = '1001' THEN 'GRANDES CLIENTES'
                ELSE cs.segment
              END), 'SIN SEGMENTO') AS segment_name,
              COUNT(*)::int AS payment_count,
              COALESCE(SUM(amount_soles), 0) AS total_amount
            FROM public.customer_payments cp
            LEFT JOIN public.customer_supplies cs ON cs.supply_code = cp.supply_code
            GROUP BY COALESCE(source_customer_name, 'SIN NOMBRE')
            ORDER BY total_amount DESC, customer_name ASC
            LIMIT 10;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            SELECT
              COUNT(*)::int AS payment_count,
              COUNT(*) FILTER (WHERE customer_id IS NOT NULL)::int AS matched_payment_count,
              COUNT(*) FILTER (WHERE customer_id IS NULL)::int AS unmatched_payment_count,
              COALESCE(SUM(amount_soles), 0) AS total_amount
            FROM public.customer_payments;
            """,
        ),
        fetch_cond(not tab or tab == "resumen" or tab == "distribucion", 
            pool,
            """
            WITH latest_snapshot AS (
              SELECT MAX(snapshot_date) AS snapshot_date
              FROM public.fp_debt_snapshots
            )
            SELECT
              fps.customer_name,
              fps.customer_code,
              COALESCE(MAX(cs.district), 'SIN DISTRITO') AS district,
              COALESCE(MAX(cs.segment), 'FUENTE PROPIA') AS segment_name,
              COALESCE(SUM(fps.debt_amount), 0) AS total_debt
            FROM public.fp_debt_snapshots fps
            LEFT JOIN public.customer_supplies cs ON cs.supply_code = fps.supply_code
            JOIN latest_snapshot ls ON ls.snapshot_date = fps.snapshot_date
            WHERE UPPER(COALESCE(fps.customer_name, '')) NOT LIKE '%%SEDAPAL%%'
              AND COALESCE(fps.office_code, '5111') = '5111'
            GROUP BY fps.customer_name, fps.customer_code
            ORDER BY total_debt DESC, fps.customer_name ASC
            LIMIT 10;
            """,
        ),
        fetch_cond(not tab or tab == "resumen", 
            pool,
            """
            WITH latest_snapshot AS (
              SELECT MAX(snapshot_date) AS snapshot_date
              FROM public.fp_debt_snapshots
            )
            SELECT
              COUNT(DISTINCT fps.customer_name)::int AS customer_count,
              COALESCE(SUM(fps.debt_amount), 0) AS total_debt
            FROM public.fp_debt_snapshots fps
            JOIN latest_snapshot ls ON ls.snapshot_date = fps.snapshot_date
            WHERE UPPER(COALESCE(fps.customer_name, '')) NOT LIKE '%%SEDAPAL%%'
              AND COALESCE(fps.office_code, '5111') = '5111';
            """,
        ),
        fetch_cond(not tab or tab == "volumenes", 
            pool,
            """
            WITH debt_periods AS (
              -- Fuente primaria: customer_debts via customer_supply_id (join correcto)
              SELECT
                CASE
                  WHEN UPPER(COALESCE(cs.segment, '')) LIKE '%%GRANDES CLIENTES%%'
                    OR COALESCE(cd.commercial_office_code, '') = '1001'
                    THEN 'GRANDES CLIENTES'
                  WHEN UPPER(COALESCE(cs.segment, '')) LIKE '%%FUENTE PROPIA%%'
                    OR COALESCE(cd.commercial_office_code, '') = '5111'
                    THEN 'FUENTE PROPIA'
                  ELSE NULL
                END AS customer_category,
                cd.period_year::int AS period_year,
                cd.period_month::int AS period_month,
                cd.billed_volume_m3::numeric AS total_volume_m3,
                1 AS source_priority
              FROM public.customer_debts cd
              LEFT JOIN public.customer_supplies cs ON cs.id = cd.customer_supply_id
              WHERE cd.period_year IS NOT NULL
                AND cd.period_month IS NOT NULL
                AND cd.billed_volume_m3 IS NOT NULL
                AND cd.billed_volume_m3 > 0
                AND COALESCE(cd.concept, '') ILIKE '%%agua%%'
              UNION ALL
              -- Fuente secundaria: billing_daily (periodos no cubiertos por customer_debts)
              SELECT
                CASE
                  WHEN csbd.office_code = '1001'
                    OR UPPER(COALESCE(cs.office_name, '')) LIKE '%%GRANDES CLIENTES%%'
                    OR UPPER(COALESCE(cs.segment, '')) LIKE '%%GRANDES CLIENTES%%'
                    THEN 'GRANDES CLIENTES'
                  WHEN csbd.office_code = '5111'
                    OR UPPER(COALESCE(cs.office_name, '')) LIKE '%%FUENTE PROPIA%%'
                    OR UPPER(COALESCE(cs.segment, '')) LIKE '%%FUENTE PROPIA%%'
                    THEN 'FUENTE PROPIA'
                  ELSE NULL
                END AS customer_category,
                EXTRACT(YEAR FROM csbd.issue_date)::int AS period_year,
                EXTRACT(MONTH FROM csbd.issue_date)::int AS period_month,
                csbd.billed_volume_m3::numeric AS total_volume_m3,
                2 AS source_priority
              FROM public.customer_supply_billing_daily csbd
              LEFT JOIN public.customer_supplies cs ON cs.supply_code = csbd.supply_code
              WHERE csbd.issue_date IS NOT NULL
                AND csbd.billed_volume_m3 IS NOT NULL
                AND csbd.billed_volume_m3 > 0
                AND COALESCE(csbd.concept, '') ILIKE '%%agua%%'
            ),
            aggregated_sources AS (
              SELECT
                source_priority,
                customer_category,
                period_year,
                period_month,
                ROUND(SUM(total_volume_m3), 2) AS total_volume_m3
              FROM debt_periods
              WHERE customer_category IS NOT NULL
                AND period_year >= 2000
                AND period_month BETWEEN 1 AND 12
              GROUP BY source_priority, customer_category, period_year, period_month
            ),
            ranked_sources AS (
              SELECT
                source_priority,
                customer_category,
                period_year,
                period_month,
                total_volume_m3,
                ROW_NUMBER() OVER (
                  PARTITION BY customer_category, period_year, period_month
                  ORDER BY source_priority ASC
                ) AS source_rank
              FROM aggregated_sources
            )
            SELECT
              customer_category,
              period_year,
              period_month,
              total_volume_m3
            FROM ranked_sources
            WHERE source_rank = 1
            ORDER BY period_year ASC, period_month ASC, customer_category ASC;
            """,
        ),
        fetch_cond(not tab or tab == "volumenes", 
            pool,
            """
            WITH billed_periods AS (
              SELECT
                CASE
                  WHEN UPPER(COALESCE(cs.segment, '')) LIKE '%%GRANDES CLIENTES%%'
                    OR COALESCE(cd.commercial_office_code, '') = '1001'
                    THEN 'GRANDES CLIENTES'
                  WHEN UPPER(COALESCE(cs.segment, '')) LIKE '%%FUENTE PROPIA%%'
                    OR COALESCE(cd.commercial_office_code, '') = '5111'
                    THEN 'FUENTE PROPIA'
                  ELSE NULL
                END AS customer_category,
                cd.period_year::int AS period_year,
                cd.period_month::int AS period_month,
                cd.total_soles::numeric AS total_amount_soles,
                1 AS source_priority
              FROM public.customer_debts cd
              LEFT JOIN public.customer_supplies cs ON cs.id = cd.customer_supply_id
              WHERE cd.period_year IS NOT NULL
                AND cd.period_month IS NOT NULL
                AND cd.total_soles IS NOT NULL
                AND cd.total_soles > 0
                AND COALESCE(cd.concept, '') ILIKE '%%agua%%'

              UNION ALL

              SELECT
                CASE
                  WHEN csbd.office_code = '1001'
                    OR UPPER(COALESCE(cs.office_name, '')) LIKE '%%GRANDES CLIENTES%%'
                    OR UPPER(COALESCE(cs.segment, '')) LIKE '%%GRANDES CLIENTES%%'
                    THEN 'GRANDES CLIENTES'
                  WHEN csbd.office_code = '5111'
                    OR UPPER(COALESCE(cs.office_name, '')) LIKE '%%FUENTE PROPIA%%'
                    OR UPPER(COALESCE(cs.segment, '')) LIKE '%%FUENTE PROPIA%%'
                    THEN 'FUENTE PROPIA'
                  ELSE NULL
                END AS customer_category,
                EXTRACT(YEAR FROM csbd.issue_date)::int AS period_year,
                EXTRACT(MONTH FROM csbd.issue_date)::int AS period_month,
                csbd.amount_soles::numeric AS total_amount_soles,
                2 AS source_priority
              FROM public.customer_supply_billing_daily csbd
              LEFT JOIN public.customer_supplies cs ON cs.supply_code = csbd.supply_code
              WHERE csbd.issue_date IS NOT NULL
                AND csbd.amount_soles IS NOT NULL
                AND csbd.amount_soles > 0
                AND COALESCE(csbd.concept, '') ILIKE '%%agua%%'
            ),
            aggregated_sources AS (
              SELECT
                source_priority,
                customer_category,
                period_year,
                period_month,
                ROUND(SUM(total_amount_soles), 2) AS total_amount_soles
              FROM billed_periods
              WHERE customer_category IS NOT NULL
                AND period_year >= 2000
                AND period_month BETWEEN 1 AND 12
              GROUP BY source_priority, customer_category, period_year, period_month
            ),
            ranked_sources AS (
              SELECT
                source_priority,
                customer_category,
                period_year,
                period_month,
                total_amount_soles,
                ROW_NUMBER() OVER (
                  PARTITION BY customer_category, period_year, period_month
                  ORDER BY source_priority ASC
                ) AS source_rank
              FROM aggregated_sources
            )
            SELECT
              customer_category,
              period_year,
              period_month,
              total_amount_soles
            FROM ranked_sources
            WHERE source_rank = 1
            ORDER BY period_year ASC, period_month ASC, customer_category ASC;
            """,
        ),
        fetch_cond(not tab or tab == "distribucion", 
            pool,
            """
            WITH latest_snapshot AS (
              SELECT MAX(snapshot_date) AS snapshot_date
              FROM public.fp_debt_snapshots
            ),
            snapshot_debt AS (
              SELECT
                COALESCE(fps.debt_amount, 0)::numeric AS debt_amount,
                GREATEST(
                  CURRENT_DATE - COALESCE(fps.due_date, fps.billing_date, fps.collection_date, fps.snapshot_date),
                  0
                )::int AS overdue_days
              FROM public.fp_debt_snapshots fps
              JOIN latest_snapshot ls ON ls.snapshot_date = fps.snapshot_date
              WHERE UPPER(COALESCE(fps.customer_name, '')) NOT LIKE '%%SEDAPAL%%'
                AND COALESCE(fps.office_code, '5111') = '5111'
            )
            SELECT
              CASE
                WHEN overdue_days < 60 THEN '1.-Deuda menor a 2 meses'
                WHEN overdue_days < 180 THEN '2.-Deuda entre 2 y 6 meses'
                WHEN overdue_days < 365 THEN '3.-Deuda entre 7 y 12 meses'
                WHEN overdue_days < 730 THEN '4.-Deuda entre 1 y 2 años'
                WHEN overdue_days < 1825 THEN '5.-Deuda entre 2 y 5 años'
                WHEN overdue_days < 3650 THEN '6.-Deuda entre 6 y 10 años'
                ELSE '7.-Deuda mayor a 10 años'
              END AS bucket_label,
              CASE
                WHEN overdue_days < 60 THEN 1
                WHEN overdue_days < 180 THEN 2
                WHEN overdue_days < 365 THEN 3
                WHEN overdue_days < 730 THEN 4
                WHEN overdue_days < 1825 THEN 5
                WHEN overdue_days < 3650 THEN 6
                ELSE 7
              END AS sort_order,
              ROUND(SUM(debt_amount), 2) AS total_debt
            FROM snapshot_debt
            GROUP BY 1, 2
            ORDER BY sort_order ASC;
            """,
        ),
        fetch_cond(not tab or tab == "distribucion", 
            pool,
            """
            WITH current_debt AS (
              SELECT
                COALESCE(cd.total_soles, 0)::numeric AS total_soles,
                NULLIF(TRIM(COALESCE(cd.commercial_office_code, '')), '') AS commercial_office_code
              FROM public.customer_debts cd
              WHERE cd.status NOT IN ('pagada', 'condonada')
                AND COALESCE(cd.total_soles, 0) > 0
            )
            SELECT
              commercial_office_code AS office_code,
              CASE
                WHEN commercial_office_code = '1001' THEN 'GRANDES CLIENTES'
                WHEN commercial_office_code = '5111' THEN 'FUENTE PROPIA'
                ELSE COALESCE(commercial_office_code, 'SIN OFICINA')
              END AS office_name,
              ROUND(SUM(total_soles), 2) AS total_debt
            FROM current_debt
            GROUP BY 1, 2
            ORDER BY total_debt DESC, office_name ASC;
            """,
        ),
        fetch_cond(not tab or tab == "distribucion", 
            pool,
            """
            WITH current_debt AS (
              SELECT
                COALESCE(cd.total_soles, 0)::numeric AS total_soles,
                cd.customer_supply_id
              FROM public.customer_debts cd
              WHERE cd.status NOT IN ('pagada', 'condonada')
                AND COALESCE(cd.total_soles, 0) > 0
            )
            SELECT
              CASE
                WHEN UPPER(COALESCE(cs.tariff, '')) = 'DOMESTICO' THEN 'Servicio domestico'
                WHEN NULLIF(TRIM(COALESCE(cs.ciiu, '')), '') IS NOT NULL THEN INITCAP(TRIM(cs.ciiu))
                WHEN NULLIF(TRIM(COALESCE(cs.tariff, '')), '') IS NOT NULL THEN INITCAP(TRIM(cs.tariff))
                ELSE 'Sin clasificar'
              END AS use_label,
              ROUND(SUM(cd.total_soles), 2) AS total_debt
            FROM current_debt cd
            LEFT JOIN public.customer_supplies cs ON cs.id = cd.customer_supply_id
            GROUP BY 1
            HAVING CASE
              WHEN UPPER(COALESCE(cs.tariff, '')) = 'DOMESTICO' THEN 'Servicio domestico'
              WHEN NULLIF(TRIM(COALESCE(cs.ciiu, '')), '') IS NOT NULL THEN INITCAP(TRIM(cs.ciiu))
              WHEN NULLIF(TRIM(COALESCE(cs.tariff, '')), '') IS NOT NULL THEN INITCAP(TRIM(cs.tariff))
              ELSE 'Sin clasificar'
            END <> 'Sin clasificar'
            ORDER BY total_debt DESC, use_label ASC
            LIMIT 10;
            """,
        ),
        fetch_cond(not tab or tab == "distribucion", 
            pool,
            """
            WITH current_debt AS (
              SELECT
                COALESCE(cd.total_soles, 0)::numeric AS total_soles,
                cd.customer_supply_id
              FROM public.customer_debts cd
              WHERE cd.status NOT IN ('pagada', 'condonada')
                AND COALESCE(cd.total_soles, 0) > 0
            )
            SELECT
              COALESCE(NULLIF(TRIM(cs.tariff), ''), 'Tarifa no identificada') AS tariff_label,
              ROUND(SUM(cd.total_soles), 2) AS total_debt
            FROM current_debt cd
            LEFT JOIN public.customer_supplies cs ON cs.id = cd.customer_supply_id
            GROUP BY 1
            ORDER BY total_debt DESC, tariff_label ASC;
            """,
        ),
        fetch_cond(not tab or tab == "distribucion", 
            pool,
            """
            WITH current_debt AS (
              SELECT
                COALESCE(cd.total_soles, 0)::numeric AS total_soles,
                cd.customer_supply_id
              FROM public.customer_debts cd
              WHERE cd.status NOT IN ('pagada', 'condonada')
                AND COALESCE(cd.total_soles, 0) > 0
            )
            SELECT
              COALESCE(
                NULLIF(TRIM(cs.zone_name), ''),
                NULLIF(TRIM(cs.district), ''),
                'Sin zonal'
              ) AS zone_label,
              ROUND(SUM(cd.total_soles), 2) AS total_debt
            FROM current_debt cd
            LEFT JOIN public.customer_supplies cs ON cs.id = cd.customer_supply_id
            GROUP BY 1
            ORDER BY total_debt DESC, zone_label ASC
            LIMIT 6;
            """,
        ),
    )

    return {
        "billedVolumeProjection": billed_volume_projection,
        "billedAmountProjection": billed_amount_projection,
        "customers": customers,
        "monthlyPayments": monthly_payments,
        "paymentSummary": {
            "offices": [
                {
                    "office_code": row.get("office_code"),
                    "office_name": map_office_name(row.get("office_code")),
                    "payment_count": row.get("payment_count"),
                    "total_amount": row.get("total_amount"),
                }
                for row in offices
            ],
            "tariffs": tariffs,
            "topPayers": [
                {
                    "customer_name": row.get("customer_name"),
                    "office_code": row.get("office_code"),
                    "office_name": map_office_name(row.get("office_code")),
                    "payment_count": row.get("payment_count"),
                    "segment_name": row.get("segment_name"),
                    "total_amount": row.get("total_amount"),
                }
                for row in top_payers
            ],
            "totals": totals[0]
            if totals
            else {
                "matched_payment_count": 0,
                "payment_count": 0,
                "total_amount": 0,
                "unmatched_payment_count": 0,
            },
        },
        "fpDebtSummary": {
            "customerCount": debt_totals[0]["customer_count"] if debt_totals else 0,
            "snapshotTotalDebt": debt_totals[0]["total_debt"] if debt_totals else 0,
            "topDebtors": top_debtors,
        },
        "debtAnalytics": {
            "ageRanges": debt_age_ranges,
            "officeTotals": debt_by_office,
            "tariffTotals": debt_by_tariff,
            "topUses": top_uses_by_debt,
            "zoneTotals": debt_by_zone,
        },
    }
