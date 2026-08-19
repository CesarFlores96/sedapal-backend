from datetime import datetime

from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict

MIN_MEDIAN_COVERAGE_RATIO = 0.7

CANONICAL_BILLING_CTES = """
        debt_ranked AS (
          SELECT
            cd.supply_code,
            cd.period_year::int AS period_year,
            cd.period_month::int AS period_month,
            cd.concept,
            COALESCE(cd.billed_volume_m3, 0)::numeric AS billed_volume_m3,
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
          WHERE cd.supply_code IS NOT NULL
            AND cd.period_year IS NOT NULL
            AND cd.period_month IS NOT NULL
        ),
        daily_ranked AS (
          SELECT
            csbd.supply_code,
            EXTRACT(YEAR FROM csbd.issue_date)::int AS period_year,
            EXTRACT(MONTH FROM csbd.issue_date)::int AS period_month,
            csbd.concept,
            COALESCE(csbd.billed_volume_m3, 0)::numeric AS billed_volume_m3,
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
          WHERE csbd.supply_code IS NOT NULL
            AND csbd.issue_date IS NOT NULL
        ),
        billing_history AS (
          SELECT
            debt.supply_code,
            debt.period_year,
            debt.period_month,
            debt.concept,
            debt.billed_volume_m3
          FROM debt_ranked debt
          WHERE debt.source_rank = 1

          UNION ALL

          SELECT
            daily.supply_code,
            daily.period_year,
            daily.period_month,
            daily.concept,
            daily.billed_volume_m3
          FROM daily_ranked daily
          WHERE daily.source_rank = 1
            AND NOT EXISTS (
              SELECT 1
              FROM debt_ranked debt
              WHERE debt.source_rank = 1
                AND debt.supply_code = daily.supply_code
                AND debt.period_year = daily.period_year
                AND debt.period_month = daily.period_month
                AND debt.concept = daily.concept
            )
        )
"""


def compute_safe_variation_percent(target_median_m3: float, baseline_median_m3: float) -> float:
    if baseline_median_m3 == 0:
        return 0.0 if target_median_m3 == 0 else 100.0
    return ((target_median_m3 - baseline_median_m3) / baseline_median_m3) * 100


def format_period_label(year: int | None, month: int | None) -> str | None:
    if not year or not month:
        return None
    date_value = datetime(year=year, month=month, day=1)
    return date_value.strftime("%b %Y")


def format_semester_label(year: int | None, semester: int | None) -> str | None:
    if not year or semester not in (1, 2):
        return None
    return f"Sem. {semester} {year}"


def parse_period_code(period_code: str) -> tuple[int, int]:
    year_text, month_text = period_code.split("-", 1)
    return int(year_text), int(month_text)


def expand_period_bounds(period_mode: str, start_period: str, end_period: str) -> tuple[str, str]:
    if period_mode == "year":
        start_year, _ = parse_period_code(start_period)
        end_year, _ = parse_period_code(end_period)
        return f"{start_year}-01", f"{end_year}-12"

    return start_period, end_period


def format_period_range_label(period_mode: str, start_period: str, end_period: str) -> str:
    if period_mode == "year":
        start_year, _ = parse_period_code(start_period)
        end_year, _ = parse_period_code(end_period)
        return f"{start_year}" if start_year == end_year else f"{start_year}-{end_year}"

    start_year, start_month = parse_period_code(start_period)
    end_year, end_month = parse_period_code(end_period)
    start_label = format_period_label(start_year, start_month) or start_period
    end_label = format_period_label(end_year, end_month) or end_period
    return start_label if start_period == end_period else f"{start_label} - {end_label}"


def normalize_search_text(value: str | None) -> str:
    return (value or "").strip()


def build_report_master_where(search: str) -> tuple[str, list[str]]:
    normalized_search = normalize_search_text(search)
    if not normalized_search:
        return "", []

    if normalized_search.isdigit():
        return (
            """
            WHERE cs.supply_code = %s
               OR cs.supply_code LIKE %s
            """,
            [normalized_search, f"%{normalized_search}%"],
        )

    search_value = f"%{normalized_search.lower()}%"
    return (
        """
        WHERE lower(cs.supply_code) LIKE %s
           OR lower(coalesce(cs.customer_name, c.business_name, c.full_name, '')) LIKE %s
           OR lower(coalesce(cs.district, c.district, '')) LIKE %s
           OR lower(coalesce(c.payer_classification::text, '')) LIKE %s
           OR lower(coalesce(cs.segment, '')) LIKE %s
           OR lower(coalesce(cs.office_name, '')) LIKE %s
        """,
        [search_value, search_value, search_value, search_value, search_value, search_value],
    )


def build_consumption_filter_clause(filter_value: dict | None) -> tuple[str, str, list]:
    if not filter_value:
        return "", "", []

    min_var_m3 = max(
        int(filter_value.get("minVarM3", filter_value.get("dropM3Min", 50)) or 0),
        0,
    )

    target_start_period, target_end_period = expand_period_bounds(
        filter_value.get("periodMode", "month"),
        filter_value["targetStartPeriod"],
        filter_value["targetEndPeriod"],
    )
    baseline_start_period, baseline_end_period = expand_period_bounds(
        filter_value.get("periodMode", "month"),
        filter_value["baselineStartPeriod"],
        filter_value["baselineEndPeriod"],
    )

    return (
        f"""
        ,
        comparison_ranges AS (
          SELECT
            to_date(%s || '-01', 'YYYY-MM-DD') AS target_start_date,
            (to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date AS target_end_date,
            to_date(%s || '-01', 'YYYY-MM-DD') AS baseline_start_date,
            (to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date AS baseline_end_date,
            (
              (date_part('year', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD'))) * 12)
              + date_part('month', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD')))
              + 1
            )::int AS target_total_months,
            (
              (date_part('year', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD'))) * 12)
              + date_part('month', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD')))
              + 1
            )::int AS baseline_total_months
        ),
        {CANONICAL_BILLING_CTES},
        monthly_water AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            make_date(period_year, period_month, 1) AS period_date,
            SUM(billed_volume_m3)::numeric AS water_volume_m3
          FROM billing_history
          WHERE concept = 'consumo_agua'
          GROUP BY supply_code, period_year, period_month, make_date(period_year, period_month, 1)
        ),
        target_window AS (
          SELECT
            month_series.period_date,
            mw.supply_code,
            mw.water_volume_m3
          FROM comparison_ranges cr
          CROSS JOIN generate_series(cr.target_start_date, cr.target_end_date, interval '1 month') AS month_series(period_date)
          LEFT JOIN monthly_water mw
            ON mw.period_date = month_series.period_date
        ),
        baseline_window AS (
          SELECT
            month_series.period_date,
            mw.supply_code,
            mw.water_volume_m3
          FROM comparison_ranges cr
          CROSS JOIN generate_series(cr.baseline_start_date, cr.baseline_end_date, interval '1 month') AS month_series(period_date)
          LEFT JOIN monthly_water mw
            ON mw.period_date = month_series.period_date
        ),
        target_medians AS (
          SELECT
            supply_code,
            COUNT(*)::int AS target_points,
            percentile_cont(0.5) within group (ORDER BY water_volume_m3) AS target_median_m3
          FROM target_window
          WHERE supply_code IS NOT NULL
          GROUP BY supply_code
        ),
        baseline_medians AS (
          SELECT
            supply_code,
            COUNT(*)::int AS baseline_points,
            percentile_cont(0.5) within group (ORDER BY water_volume_m3) AS baseline_median_m3
          FROM baseline_window
          WHERE supply_code IS NOT NULL
          GROUP BY supply_code
        ),
        median_comparison AS (
          SELECT
            target_medians.supply_code,
            target_medians.target_points,
            target_medians.target_median_m3,
            baseline_medians.baseline_points,
            baseline_medians.baseline_median_m3,
            cr.target_total_months,
            cr.baseline_total_months,
            target_medians.target_median_m3 - baseline_medians.baseline_median_m3 AS delta_m3,
            CASE
              WHEN baseline_medians.baseline_median_m3 IS NULL OR target_medians.target_median_m3 IS NULL THEN NULL
              WHEN baseline_medians.baseline_median_m3 = 0 AND target_medians.target_median_m3 = 0 THEN 0
              WHEN baseline_medians.baseline_median_m3 = 0 AND target_medians.target_median_m3 > 0 THEN 100
              ELSE ((target_medians.target_median_m3 - baseline_medians.baseline_median_m3) / baseline_medians.baseline_median_m3) * 100
            END AS delta_percent,
            CASE
              WHEN baseline_medians.baseline_median_m3 IS NULL THEN NULL
              ELSE baseline_medians.baseline_median_m3 - target_medians.target_median_m3
            END AS drop_m3,
            CASE
              WHEN baseline_medians.baseline_median_m3 IS NULL OR baseline_medians.baseline_median_m3 = 0 THEN NULL
              ELSE ((baseline_medians.baseline_median_m3 - target_medians.target_median_m3) / baseline_medians.baseline_median_m3) * 100
            END AS drop_percent
          FROM target_medians
          JOIN baseline_medians ON baseline_medians.supply_code = target_medians.supply_code
          CROSS JOIN comparison_ranges cr
        ),
        filtered_supplies AS (
          SELECT
            supply_code,
            abs(delta_percent) AS sort_delta_percent,
            abs(delta_m3) AS sort_delta_m3,
            (abs(delta_percent) * 10) + abs(delta_m3) AS sort_priority_score
          FROM median_comparison
          WHERE baseline_median_m3 >= %s
            AND baseline_total_months > 0
            AND target_total_months > 0
            AND (baseline_points::numeric / baseline_total_months::numeric) >= %s
            AND (target_points::numeric / target_total_months::numeric) >= %s
            AND delta_percent IS NOT NULL
            AND delta_m3 IS NOT NULL
            AND (
              %s = 'either'
              OR (%s = 'increasing' AND delta_percent > 0)
              OR (%s = 'decreasing' AND delta_percent < 0)
            )
            AND abs(delta_percent) >= %s
            AND (%s::numeric IS NULL OR abs(delta_percent) <= %s::numeric)
            AND abs(delta_m3) >= %s
            AND (%s::numeric IS NULL OR abs(delta_m3) <= %s::numeric)
        )
        """,
        """
        JOIN filtered_supplies fs ON fs.supply_code = cs.supply_code
        """,
        [
            target_start_period,
            target_end_period,
            baseline_start_period,
            baseline_end_period,
            target_end_period,
            target_start_period,
            target_end_period,
            target_start_period,
            baseline_end_period,
            baseline_start_period,
            baseline_end_period,
            baseline_start_period,
            max(int(filter_value.get("baselineVolumeM3", 0)), 0),
            MIN_MEDIAN_COVERAGE_RATIO,
            MIN_MEDIAN_COVERAGE_RATIO,
            filter_value.get("trendDirection", "either"),
            filter_value.get("trendDirection", "either"),
            filter_value.get("trendDirection", "either"),
            max(int(filter_value.get("dropPercentMin", 0)), 0),
            filter_value.get("dropPercentMax"),
            filter_value.get("dropPercentMax"),
            min_var_m3,
            filter_value.get("dropM3Max"),
            filter_value.get("dropM3Max"),
        ],
    )


async def fetch_report_master_page(
    pool: AsyncConnectionPool,
    *,
    page: int,
    page_size: int,
    search: str,
    consumption_filter: dict | None,
) -> dict:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    offset = (safe_page - 1) * safe_page_size
    where_sql, where_params = build_report_master_where(search)
    extra_cte_sql, join_sql, consumption_params = build_consumption_filter_clause(consumption_filter)
    params = [*consumption_params, *where_params]
    sort_delta_percent_sql = (
        "fs.sort_delta_percent"
        if consumption_filter
        else "NULL::numeric"
    )
    sort_delta_m3_sql = (
        "fs.sort_delta_m3"
        if consumption_filter
        else "NULL::numeric"
    )
    sort_priority_score_sql = (
        "fs.sort_priority_score"
        if consumption_filter
        else "NULL::numeric"
    )

    base_cte = f"""
    WITH debt_by_supply AS (
      SELECT
        customer_supply_id,
        SUM(total_soles) AS supply_debt_soles
      FROM public.customer_debts
      WHERE status NOT IN ('pagada', 'condonada')
      GROUP BY customer_supply_id
    ){extra_cte_sql},
    base AS (
      SELECT
        cs.supply_code,
        coalesce(cs.customer_name, c.business_name, c.full_name, 'SUMINISTRO ' || cs.supply_code) AS customer_name,
        coalesce(cs.district, c.district, 'SIN DISTRITO') AS district,
        c.payer_classification,
        coalesce(cs.geolocation_address, cs.service_address, c.address_service, 'Direccion pendiente de validacion') AS geolocation_address,
        coalesce(dbs.supply_debt_soles, 0)::numeric AS supply_debt_soles,
        {sort_delta_percent_sql} AS consumption_sort_delta_percent,
        {sort_delta_m3_sql} AS consumption_sort_delta_m3,
        {sort_priority_score_sql} AS consumption_sort_priority_score,
        cs.office_name,
        cs.segment AS segment_name,
        cs.meter_installation_date::text AS meter_installation_date,
        cs.meter_code AS meter_serial,
        cs.route_code,
        cs.itinerary_code,
        c.contact_name,
        c.email_primary,
        coalesce(c.phone_whatsapp, c.phone_primary, c.phone_secondary) AS phone,
        cs.latitude,
        cs.longitude
      FROM public.customer_supplies cs
      JOIN public.customers c ON c.id = cs.customer_id
      LEFT JOIN debt_by_supply dbs ON dbs.customer_supply_id = cs.id
      {join_sql}
      {where_sql}
    )
    """

    summary_rows = await fetch_all_dict(
        pool,
        f"""
        {base_cte}
        SELECT
          count(*)::int AS total,
          coalesce(sum(supply_debt_soles), 0)::numeric AS total_debt,
          coalesce(
            sum(
              CASE
                WHEN lower(concat_ws(' ', coalesce(segment_name, ''), coalesce(office_name, ''))) LIKE '%%grandes clientes%%'
                THEN supply_debt_soles
                ELSE 0
              END
            ),
            0
          )::numeric AS grandes_clientes_debt,
          coalesce(
            sum(
              CASE
                WHEN lower(concat_ws(' ', coalesce(segment_name, ''), coalesce(office_name, ''))) LIKE '%%fuente propia%%'
                THEN supply_debt_soles
                ELSE 0
              END
            ),
            0
          )::numeric AS fuente_propia_debt
        FROM base;
        """,
        params,
    )

    data_rows = await fetch_all_dict(
        pool,
        f"""
        {base_cte}
        SELECT
          supply_code,
          customer_name,
          district,
          payer_classification,
          geolocation_address AS address,
          supply_debt_soles AS debt,
          office_name,
          segment_name,
          meter_installation_date,
          meter_serial,
          route_code,
          itinerary_code,
          contact_name,
          email_primary AS email,
          phone,
          latitude,
          longitude
        FROM base
        ORDER BY
          coalesce(consumption_sort_priority_score, -1) DESC,
          coalesce(consumption_sort_delta_percent, -1) DESC,
          coalesce(consumption_sort_delta_m3, -1) DESC,
          customer_name ASC,
          supply_code ASC
        LIMIT %s
        OFFSET %s;
        """,
        [*params, safe_page_size, offset],
    )

    summary = summary_rows[0] if summary_rows else {}

    return {
        "data": data_rows,
        "page": safe_page,
        "pageSize": safe_page_size,
        "summary": {
            "fuentePropiaDebt": summary.get("fuente_propia_debt", 0),
            "grandesClientesDebt": summary.get("grandes_clientes_debt", 0),
            "totalDebt": summary.get("total_debt", 0),
        },
        "total": summary.get("total", 0),
    }


async def fetch_report_supply_meters(
    pool: AsyncConnectionPool,
    *,
    supply_code: str,
) -> list[dict]:
    table_rows = await fetch_all_dict(
        pool,
        """
        SELECT EXISTS (
          SELECT 1
          FROM information_schema.tables
          WHERE table_schema = 'public' AND table_name = 'meter_park_snapshots'
        ) AS present;
        """,
    )
    has_snapshot_table = bool(table_rows and table_rows[0].get("present"))

    if has_snapshot_table:
        return await fetch_all_dict(
            pool,
            """
            SELECT
              supply_code,
              master_customer_name,
              meter_serial,
              current_reading,
              previous_meter_serial,
              previous_reading,
              diameter_mm,
              installation_date::text,
              process_date::text,
              state_indicator,
              error_indicator,
              request_date::text,
              status_code,
              office_code,
              work_order_number,
              service_order_number,
              average_consumption_m3,
              useful_life_observation,
              supply_status,
              segment_name
            FROM public.meter_park_snapshots
            WHERE supply_code = %s
            ORDER BY installation_date DESC NULLS LAST, process_date DESC NULLS LAST
            LIMIT 100;
            """,
            [supply_code],
        )

    return await fetch_all_dict(
        pool,
        """
        SELECT
          cs.supply_code,
          coalesce(cs.customer_name, c.business_name, c.full_name) AS master_customer_name,
          cs.meter_code AS meter_serial,
          NULL::numeric AS current_reading,
          NULL::text AS previous_meter_serial,
          NULL::numeric AS previous_reading,
          NULL::int AS diameter_mm,
          cs.meter_installation_date::text AS installation_date,
          NULL::date::text AS process_date,
          NULL::int AS state_indicator,
          NULL::text AS error_indicator,
          NULL::timestamptz::text AS request_date,
          NULL::text AS status_code,
          cs.office_name AS office_code,
          NULL::bigint AS work_order_number,
          NULL::bigint AS service_order_number,
          NULL::numeric AS average_consumption_m3,
          cs.meter_status AS useful_life_observation,
          cs.supply_status,
          cs.segment AS segment_name
        FROM public.customer_supplies cs
        JOIN public.customers c ON c.id = cs.customer_id
        WHERE cs.supply_code = %s
        ORDER BY cs.supply_code
        LIMIT 100;
        """,
        [supply_code],
    )


async def fetch_report_supply_anomalies(
    pool: AsyncConnectionPool,
    *,
    supply_code: str,
) -> list[dict]:
    return await fetch_all_dict(
        pool,
        """
        SELECT
          a.id::text AS anomaly_id,
          a.anomaly_type::text AS anomaly_type,
          a.detected_at::text AS detected_at,
          a.detected_value,
          a.expected_value,
          a.deviation_pct,
          a.resolved,
          a.resolved_at::text AS resolved_at,
          a.resolution_notes,
          m.serial AS meter_serial,
          wo.code AS work_order_code,
          wo.status::text AS work_order_status,
          mr.reading_date::text AS reading_date,
          mr.reading_value,
          mr.previous_value
        FROM public.anomalies a
        LEFT JOIN public.meters m
          ON m.id = a.meter_id
        LEFT JOIN public.work_orders wo
          ON wo.id = a.work_order_id
        LEFT JOIN public.meter_readings mr
          ON mr.id = a.reading_id
        LEFT JOIN public.domestic_connections dc
          ON dc.id = COALESCE(m.connection_id, wo.connection_id)
        WHERE COALESCE(a.supply_code, dc.supply_code_ref) = %s
        ORDER BY a.detected_at DESC NULLS LAST, a.created_at DESC NULLS LAST
        LIMIT 100;
        """,
        [supply_code],
    )


async def fetch_consumption_anomalies(
    pool: AsyncConnectionPool,
    *,
    baseline_end_period: str,
    baseline_start_period: str,
    baseline_volume_m3: int,
    drop_m3_max: int | None,
    min_var_m3: int,
    drop_percent_max: int | None,
    drop_percent_min: int,
    match_mode: str,
    period_mode: str,
    target_end_period: str,
    target_start_period: str,
    trend_direction: str,
) -> dict:
    target_start_period, target_end_period = expand_period_bounds(
        period_mode,
        target_start_period,
        target_end_period,
    )
    baseline_start_period, baseline_end_period = expand_period_bounds(
        period_mode,
        baseline_start_period,
        baseline_end_period,
    )

    rows = await fetch_all_dict(
        pool,
        f"""
        WITH comparison_ranges AS (
          SELECT
            to_date(%s || '-01', 'YYYY-MM-DD') AS target_start_date,
            (to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date AS target_end_date,
            to_date(%s || '-01', 'YYYY-MM-DD') AS baseline_start_date,
            (to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date AS baseline_end_date,
            (
              (date_part('year', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD'))) * 12)
              + date_part('month', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD')))
              + 1
            )::int AS target_total_months,
            (
              (date_part('year', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD'))) * 12)
              + date_part('month', age((to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date, to_date(%s || '-01', 'YYYY-MM-DD')))
              + 1
            )::int AS baseline_total_months
        ),
        {CANONICAL_BILLING_CTES},
        monthly_water AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            make_date(period_year, period_month, 1) AS period_date,
            SUM(billed_volume_m3)::numeric AS water_volume_m3
          FROM billing_history
          WHERE concept = 'consumo_agua'
          GROUP BY supply_code, period_year, period_month, make_date(period_year, period_month, 1)
        ),
        target_window AS (
          SELECT
            month_series.period_date,
            mw.supply_code,
            mw.water_volume_m3
          FROM comparison_ranges cr
          CROSS JOIN generate_series(cr.target_start_date, cr.target_end_date, interval '1 month') AS month_series(period_date)
          LEFT JOIN monthly_water mw
            ON mw.period_date = month_series.period_date
        ),
        baseline_window AS (
          SELECT
            month_series.period_date,
            mw.supply_code,
            mw.water_volume_m3
          FROM comparison_ranges cr
          CROSS JOIN generate_series(cr.baseline_start_date, cr.baseline_end_date, interval '1 month') AS month_series(period_date)
          LEFT JOIN monthly_water mw
            ON mw.period_date = month_series.period_date
        ),
        target_medians AS (
          SELECT
            supply_code,
            COUNT(*)::int AS target_points,
            percentile_cont(0.5) within group (ORDER BY water_volume_m3) AS target_median_m3
          FROM target_window
          WHERE supply_code IS NOT NULL
          GROUP BY supply_code
        ),
        baseline_medians AS (
          SELECT
            supply_code,
            COUNT(*)::int AS baseline_points,
            percentile_cont(0.5) within group (ORDER BY water_volume_m3) AS baseline_median_m3
          FROM baseline_window
          WHERE supply_code IS NOT NULL
          GROUP BY supply_code
        )
        SELECT
          target_medians.supply_code,
          target_medians.target_points,
          target_medians.target_median_m3,
          baseline_medians.baseline_points,
          baseline_medians.baseline_median_m3,
          cr.target_total_months,
          cr.baseline_total_months,
          target_medians.target_median_m3 - baseline_medians.baseline_median_m3 AS delta_m3,
          CASE
            WHEN baseline_medians.baseline_median_m3 IS NULL OR target_medians.target_median_m3 IS NULL THEN NULL
            WHEN baseline_medians.baseline_median_m3 = 0 AND target_medians.target_median_m3 = 0 THEN 0
            WHEN baseline_medians.baseline_median_m3 = 0 AND target_medians.target_median_m3 > 0 THEN 100
            ELSE ((target_medians.target_median_m3 - baseline_medians.baseline_median_m3) / baseline_medians.baseline_median_m3) * 100
          END AS delta_percent,
          CASE
            WHEN baseline_medians.baseline_median_m3 IS NULL THEN NULL
            ELSE baseline_medians.baseline_median_m3 - target_medians.target_median_m3
          END AS drop_m3,
          CASE
            WHEN baseline_medians.baseline_median_m3 IS NULL OR baseline_medians.baseline_median_m3 = 0 THEN NULL
            ELSE ((baseline_medians.baseline_median_m3 - target_medians.target_median_m3) / baseline_medians.baseline_median_m3) * 100
          END AS drop_percent
        FROM target_medians
        JOIN baseline_medians ON baseline_medians.supply_code = target_medians.supply_code
        CROSS JOIN comparison_ranges cr
        WHERE baseline_medians.baseline_median_m3 >= %s
          AND cr.baseline_total_months > 0
          AND cr.target_total_months > 0
          AND (baseline_medians.baseline_points::numeric / cr.baseline_total_months::numeric) >= %s
          AND (target_medians.target_points::numeric / cr.target_total_months::numeric) >= %s
        ORDER BY target_medians.supply_code ASC;
        """,
        [
            target_start_period,
            target_end_period,
            baseline_start_period,
            baseline_end_period,
            target_end_period,
            target_start_period,
            target_end_period,
            target_start_period,
            baseline_end_period,
            baseline_start_period,
            baseline_end_period,
            baseline_start_period,
            baseline_volume_m3,
            MIN_MEDIAN_COVERAGE_RATIO,
            MIN_MEDIAN_COVERAGE_RATIO,
        ],
    )

    insights_by_supply: dict[str, dict] = {}

    for row in rows:
        current_volume_m3 = float(row.get("target_median_m3") or 0)
        baseline_median_m3 = (
            None if row.get("baseline_median_m3") is None else float(row["baseline_median_m3"])
        )
        latest_vs_median_m3 = None
        latest_vs_median_percent = None

        if baseline_median_m3 is not None:
            latest_vs_median_m3 = current_volume_m3 - baseline_median_m3
            latest_vs_median_percent = compute_safe_variation_percent(
                current_volume_m3,
                baseline_median_m3,
            )

        drop_m3 = None if row.get("drop_m3") is None else float(row["drop_m3"])
        drop_percent = None if row.get("drop_percent") is None else float(row["drop_percent"])

        is_increasing = latest_vs_median_percent is not None and latest_vs_median_percent > 0
        is_decreasing = latest_vs_median_percent is not None and latest_vs_median_percent < 0

        matches_direction = (
            trend_direction == "either"
            or (trend_direction == "increasing" and is_increasing)
            or (trend_direction == "decreasing" and is_decreasing)
        )

        has_decline = (
            matches_direction
            and latest_vs_median_percent is not None
            and abs(latest_vs_median_percent) >= drop_percent_min
            and (drop_percent_max is None or abs(latest_vs_median_percent) <= drop_percent_max)
        )
        has_abrupt_change = (
            matches_direction
            and latest_vs_median_m3 is not None
            and abs(latest_vs_median_m3) >= min_var_m3
            and (drop_m3_max is None or abs(latest_vs_median_m3) <= drop_m3_max)
        )

        qualifies = has_decline and has_abrupt_change

        if not qualifies:
            continue

        current_period_label = format_period_range_label(
            period_mode,
            target_start_period,
            target_end_period,
        )
        previous_period_label = format_period_range_label(
            period_mode,
            baseline_start_period,
            baseline_end_period,
        )

        insights_by_supply[row["supply_code"]] = {
            "abruptEventCount": 1 if has_abrupt_change else 0,
            "baselineCount": int(row.get("baseline_points") or 0),
            "baselineMedianM3": baseline_median_m3,
            "currentPeriodLabel": current_period_label,
            "currentVolumeM3": current_volume_m3,
            "hasAbruptChange": has_abrupt_change,
            "hasDecline": has_decline,
            "latestDeltaM3": latest_vs_median_m3,
            "latestDeltaPercent": latest_vs_median_percent,
            "latestPeriodLabel": current_period_label,
            "latestVsMedianM3": latest_vs_median_m3,
            "latestVsMedianPercent": latest_vs_median_percent,
            "previousPeriodLabel": previous_period_label,
            "previousVolumeM3": baseline_median_m3,
            "strongestEventLabel": current_period_label,
            "strongestEventType": "abrupt" if has_abrupt_change else "decline",
            "supplyCode": row["supply_code"],
            "worstDeltaM3": latest_vs_median_m3,
            "worstDeltaPercent": latest_vs_median_percent,
        }

    insights = list(insights_by_supply.values())
    insights.sort(
        key=lambda item: abs(item.get("worstDeltaPercent") or 0) * 10 + abs(item.get("worstDeltaM3") or 0),
        reverse=True,
    )

    return {
        "filters": {
            "baselineVolumeM3": baseline_volume_m3,
            "baselineEndPeriod": baseline_end_period,
            "baselineRangeLabel": format_period_range_label(period_mode, baseline_start_period, baseline_end_period),
            "baselineStartPeriod": baseline_start_period,
            "dropM3Max": drop_m3_max,
            "dropPercentMax": drop_percent_max,
            "dropPercentMin": drop_percent_min,
            "minVarM3": min_var_m3,
            "periodMode": period_mode,
            "trendDirection": trend_direction,
            "targetEndPeriod": target_end_period,
            "targetRangeLabel": format_period_range_label(period_mode, target_start_period, target_end_period),
            "targetStartPeriod": target_start_period,
        },
        "summary": {
            "increasingMatches": len([item for item in insights if (item.get("latestDeltaPercent") or 0) > 0]),
            "decreasingMatches": len([item for item in insights if (item.get("latestDeltaPercent") or 0) < 0]),
            "totalMatches": len(insights),
        },
        "insights": insights,
    }


async def fetch_consumption_patterns(
    pool: AsyncConnectionPool,
    *,
    start_period: str,
    end_period: str,
    baseline_volume_m3: int,
) -> dict:
    start_year, start_month = parse_period_code(start_period)
    end_year, end_month = parse_period_code(end_period)

    rows = await fetch_all_dict(
        pool,
        f"""
        WITH {CANONICAL_BILLING_CTES},
        monthly_water AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            make_date(period_year, period_month, 1) AS period_date,
            SUM(billed_volume_m3)::numeric AS water_volume_m3
          FROM billing_history
          WHERE supply_code IS NOT NULL
            AND concept = 'consumo_agua'
            AND period_year IS NOT NULL
            AND period_month IS NOT NULL
            AND period_year >= 2000
            AND period_month BETWEEN 1 AND 12
            AND make_date(period_year, period_month, 1) < date_trunc('month', current_date)::date
          GROUP BY supply_code, period_year, period_month, make_date(period_year, period_month, 1)
        ),
        windowed AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            period_date,
            water_volume_m3
          FROM monthly_water
          WHERE period_date BETWEEN make_date(%s, %s, 1) AND make_date(%s, %s, 1)
        ),
        sequenced AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            period_date,
            water_volume_m3 AS current_volume_m3,
            LAG(water_volume_m3, 1) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS previous_volume_m3,
            LAG(water_volume_m3, 2) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS first_volume_m3,
            LAG(period_year, 1) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS previous_year,
            LAG(period_month, 1) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS previous_month,
            LAG(period_year, 2) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS first_year,
            LAG(period_month, 2) OVER (
              PARTITION BY supply_code
              ORDER BY period_date ASC
            ) AS first_month,
            ROW_NUMBER() OVER (
              PARTITION BY supply_code
              ORDER BY period_date DESC
            ) AS rank_desc
          FROM windowed
        )
        SELECT
          supply_code,
          first_year,
          first_month,
          first_volume_m3,
          previous_year,
          previous_month,
          previous_volume_m3,
          period_year AS current_year,
          period_month AS current_month,
          current_volume_m3,
          current_volume_m3 - previous_volume_m3 AS latest_delta_m3,
          previous_volume_m3 - first_volume_m3 AS prior_delta_m3,
          CASE
            WHEN previous_volume_m3 IS NULL OR previous_volume_m3 = 0 THEN NULL
            ELSE ((current_volume_m3 - previous_volume_m3) / previous_volume_m3) * 100
          END AS latest_delta_percent,
          CASE
            WHEN first_volume_m3 IS NULL OR first_volume_m3 = 0 THEN NULL
            ELSE ((previous_volume_m3 - first_volume_m3) / first_volume_m3) * 100
          END AS prior_delta_percent
        FROM sequenced
        WHERE rank_desc = 1
          AND first_volume_m3 IS NOT NULL
          AND previous_volume_m3 IS NOT NULL
          AND current_volume_m3 < previous_volume_m3
          AND previous_volume_m3 < first_volume_m3
          AND GREATEST(previous_volume_m3, first_volume_m3) >= %s
        ORDER BY supply_code ASC;
        """,
        [start_year, start_month, end_year, end_month, baseline_volume_m3],
    )

    insights = []

    for row in rows:
        first_volume_m3 = float(row.get("first_volume_m3") or 0)
        previous_volume_m3 = float(row.get("previous_volume_m3") or 0)
        current_volume_m3 = float(row.get("current_volume_m3") or 0)
        latest_delta_m3 = float(row.get("latest_delta_m3") or 0)
        prior_delta_m3 = float(row.get("prior_delta_m3") or 0)
        latest_delta_percent = (
            None if row.get("latest_delta_percent") is None else float(row["latest_delta_percent"])
        )
        prior_delta_percent = (
            None if row.get("prior_delta_percent") is None else float(row["prior_delta_percent"])
        )

        insights.append(
            {
                "currentPeriodLabel": format_period_label(
                    int(row["current_year"]) if row.get("current_year") is not None else None,
                    int(row["current_month"]) if row.get("current_month") is not None else None,
                ),
                "currentVolumeM3": current_volume_m3,
                "firstPeriodLabel": format_period_label(
                    int(row["first_year"]) if row.get("first_year") is not None else None,
                    int(row["first_month"]) if row.get("first_month") is not None else None,
                ),
                "firstVolumeM3": first_volume_m3,
                "hasSimultaneousReduction": True,
                "latestDeltaM3": latest_delta_m3,
                "latestDeltaPercent": latest_delta_percent,
                "previousPeriodLabel": format_period_label(
                    int(row["previous_year"]) if row.get("previous_year") is not None else None,
                    int(row["previous_month"]) if row.get("previous_month") is not None else None,
                ),
                "previousVolumeM3": previous_volume_m3,
                "priorDeltaM3": prior_delta_m3,
                "priorDeltaPercent": prior_delta_percent,
                "supplyCode": row["supply_code"],
            }
        )

    insights.sort(
        key=lambda item: abs(item.get("latestDeltaPercent") or 0) + abs(item.get("priorDeltaPercent") or 0),
        reverse=True,
    )

    return {
        "filters": {
            "baselineVolumeM3": baseline_volume_m3,
            "endPeriod": end_period,
            "endPeriodLabel": format_period_label(end_year, end_month),
            "startPeriod": start_period,
            "startPeriodLabel": format_period_label(start_year, start_month),
        },
        "summary": {
            "totalMatches": len(insights),
        },
        "insights": insights,
    }
