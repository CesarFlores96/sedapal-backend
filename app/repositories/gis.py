from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


def segment_color(segment: str | None) -> str:
    return "#0f766e" if segment == "FUENTE PROPIA" else "#1d4ed8"


def build_customer_category_sql() -> str:
    return """
        CASE
            WHEN UPPER(COALESCE(cs.office_name, '')) LIKE '%%GRANDES CLIENTES%%'
              OR UPPER(COALESCE(cs.segment, '')) LIKE '%%GRANDES CLIENTES%%'
              THEN 'GRANDES CLIENTES'
            WHEN UPPER(COALESCE(cs.office_name, '')) LIKE '%%FUENTE PROPIA%%'
              OR UPPER(COALESCE(cs.segment, '')) LIKE '%%FUENTE PROPIA%%'
              THEN 'FUENTE PROPIA'
            ELSE NULL
        END
    """


async def fetch_operational_gis_data(
    pool: AsyncConnectionPool,
    *,
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lng: float | None = None,
    max_lng: float | None = None,
    page: int = 1,
    page_size: int = 500,
    include_pipes: bool = False,
) -> dict:
    offset = max(page - 1, 0) * page_size

    bbox_clause = ""
    bbox_params: list[float] = []
    if None not in (min_lat, max_lat, min_lng, max_lng):
        bbox_clause = """
            AND cs.latitude BETWEEN %s AND %s
            AND cs.longitude BETWEEN %s AND %s
        """
        bbox_params = [float(min_lat), float(max_lat), float(min_lng), float(max_lng)]

    category_sql = build_customer_category_sql()
    base_from_sql = f"""
        FROM public.customer_supplies cs
        JOIN public.customers c ON c.id = cs.customer_id
        LEFT JOIN debt_by_supply dbs ON dbs.customer_supply_id = cs.id
        WHERE cs.latitude IS NOT NULL
          AND cs.longitude IS NOT NULL
          AND ({category_sql}) IS NOT NULL
          {bbox_clause}
    """

    total_rows = await fetch_all_dict(
        pool,
        f"""
        WITH debt_by_supply AS (
            SELECT customer_supply_id, SUM(total_soles) AS supply_debt_soles
            FROM public.customer_debts
            WHERE status NOT IN ('pagada', 'condonada')
            GROUP BY customer_supply_id
        )
        SELECT COUNT(*)::int AS total
        {base_from_sql};
        """,
        bbox_params,
    )
    total = int(total_rows[0]["total"]) if total_rows else 0

    supply_rows = await fetch_all_dict(
        pool,
        f"""
        WITH debt_by_supply AS (
            SELECT customer_supply_id, SUM(total_soles) AS supply_debt_soles
            FROM public.customer_debts
            WHERE status NOT IN ('pagada', 'condonada')
            GROUP BY customer_supply_id
        )
        SELECT
            cs.supply_code,
            COALESCE(cs.customer_name, c.business_name, c.full_name) AS customer_name,
            c.payer_classification,
            COALESCE(cs.id_doc_number, c.id_doc_number) AS id_doc_number,
            {category_sql} AS customer_category,
            cs.district,
            COALESCE(cs.geolocation_address, cs.service_address) AS geolocation_address,
            cs.location_quality,
            cs.latitude AS map_latitude,
            cs.longitude AS map_longitude,
            cs.meter_code AS meter_serial,
            COALESCE(dbs.supply_debt_soles, 0) AS supply_debt_soles,
            cs.segment,
            cs.office_name
        {base_from_sql}
        ORDER BY cs.supply_code ASC
        LIMIT %s OFFSET %s;
        """,
        [*bbox_params, page_size, offset],
    )

    pipe_rows = []
    if include_pipes:
        pipe_rows = await fetch_all_dict(
            pool,
            """
            SELECT
                id,
                network_type,
                network_level,
                material,
                diameter_mm,
                condition,
                notes,
                COALESCE(length_m, ST_Length(geom::geography)) AS computed_length_m,
                ST_AsGeoJSON(geom)::json AS geojson
            FROM public.network_pipes
            WHERE geom IS NOT NULL;
            """,
        )

    return {
        "hasMore": offset + len(supply_rows) < total,
        "page": page,
        "pageSize": page_size,
        "pipeData": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": row["geojson"],
                    "properties": {
                        "condition": row.get("condition") or "sin condicion",
                        "diameter": row.get("diameter_mm") or 0,
                        "length": float(row.get("computed_length_m") or 0),
                        "material": row.get("material") or "sin material",
                        "networkLevel": row.get("network_level") or "sin nivel",
                        "networkType": row.get("network_type") or "agua_potable",
                        "notes": row.get("notes") or "",
                    },
                }
                for row in pipe_rows
            ],
        },
        "supplyData": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            float(row["map_longitude"]),
                            float(row["map_latitude"]),
                        ],
                    },
                    "properties": {
                        "address": row.get("geolocation_address") or "Sin direccion",
                        "classification": row.get("payer_classification") or "regular",
                        "debt": float(row.get("supply_debt_soles") or 0),
                        "district": row.get("district") or "SIN DISTRITO",
                        "locationQuality": row.get("location_quality") or "sin_ubicacion",
                        "meter": row.get("meter_serial") or "Sin medidor",
                        "name": row.get("customer_name") or "Cliente sin nombre",
                        "segment": row.get("customer_category") or "Sin segmentar",
                        "segmentCode": row.get("customer_category") or "sin_segmentar",
                        "segmentColor": segment_color(row.get("customer_category")),
                        "sourceSegment": row.get("segment") or "Sin segmentar",
                        "sourceOffice": row.get("office_name") or "Sin oficina",
                        "supply": row.get("supply_code"),
                        "docNumber": row.get("id_doc_number") or "S/D",
                    },
                }
                for row in supply_rows
            ],
        },
        "total": total,
        "totalPages": max(1, (total + page_size - 1) // page_size),
    }


async def fetch_anomalies_gis_data(pool: AsyncConnectionPool) -> dict:
    rows = await fetch_all_dict(
        pool,
        """
        WITH billing_history AS (
          SELECT
            supply_code,
            period_year::int AS period_year,
            period_month::int AS period_month,
            concept,
            COALESCE(billed_volume_m3, 0)::numeric AS billed_volume_m3
          FROM public.customer_debts
          UNION ALL
          SELECT
            supply_code,
            EXTRACT(YEAR FROM issue_date)::int AS period_year,
            EXTRACT(MONTH FROM issue_date)::int AS period_month,
            concept,
            COALESCE(billed_volume_m3, 0)::numeric AS billed_volume_m3
          FROM public.customer_supply_billing_daily
        ),
        monthly_water AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            SUM(
              CASE
                WHEN lower(COALESCE(concept, 'agua')) = 'alcantarillado' THEN 0
                ELSE billed_volume_m3
              END
            )::numeric AS water_volume_m3
          FROM billing_history
          WHERE supply_code IS NOT NULL
            AND make_date(period_year, period_month, 1) < date_trunc('month', current_date)::date
          GROUP BY supply_code, period_year, period_month
        ),
        ordered AS (
          SELECT
            supply_code,
            period_year,
            period_month,
            water_volume_m3,
            ROW_NUMBER() OVER (
              PARTITION BY supply_code
              ORDER BY period_year DESC, period_month DESC
            ) AS rank_recent
          FROM monthly_water
        ),
        latest_period AS (
          SELECT supply_code, period_year, period_month, water_volume_m3
          FROM ordered
          WHERE rank_recent = 1
        ),
        previous_period AS (
          SELECT supply_code, period_year, period_month, water_volume_m3
          FROM ordered
          WHERE rank_recent = 2
        ),
        baseline_window AS (
          SELECT
            supply_code,
            COUNT(*)::int AS baseline_count,
            percentile_cont(0.5) within group (ORDER BY water_volume_m3) AS baseline_median_m3,
            AVG(water_volume_m3)::numeric AS baseline_avg_m3
          FROM ordered
          WHERE rank_recent BETWEEN 2 AND 6
          GROUP BY supply_code
        )
        SELECT
          latest.supply_code,
          cs.latitude,
          cs.longitude,
          cs.district,
          COALESCE(cs.customer_name, c.business_name, c.full_name) AS customer_name,
          latest.water_volume_m3 AS current_volume_m3,
          previous.water_volume_m3 AS previous_volume_m3,
          baseline.baseline_median_m3,
          latest.water_volume_m3 - previous.water_volume_m3 AS latest_vs_previous_m3,
          ((latest.water_volume_m3 - previous.water_volume_m3) / NULLIF(previous.water_volume_m3, 0)) * 100 AS latest_vs_previous_percent,
          latest.water_volume_m3 - baseline.baseline_median_m3 AS latest_vs_median_m3,
          ((latest.water_volume_m3 - baseline.baseline_median_m3) / NULLIF(baseline.baseline_median_m3, 0)) * 100 AS latest_vs_median_percent
        FROM latest_period latest
        LEFT JOIN previous_period previous ON previous.supply_code = latest.supply_code
        LEFT JOIN baseline_window baseline ON baseline.supply_code = latest.supply_code
        JOIN public.customer_supplies cs ON cs.supply_code = latest.supply_code
        JOIN public.customers c ON c.id = cs.customer_id
        WHERE COALESCE(previous.water_volume_m3, baseline.baseline_median_m3, 0) >= 100
          AND cs.latitude IS NOT NULL
          AND cs.longitude IS NOT NULL;
        """,
    )

    features = []
    decline_percent = 30
    abrupt_change_m3 = 300

    for row in rows:
        lat = float(row["latitude"])
        lng = float(row["longitude"])

        latest_vs_previous_percent = (
            float(row["latest_vs_previous_percent"])
            if row["latest_vs_previous_percent"] is not None
            else None
        )
        latest_vs_median_percent = (
            float(row["latest_vs_median_percent"])
            if row["latest_vs_median_percent"] is not None
            else None
        )
        latest_vs_previous_m3 = (
            float(row["latest_vs_previous_m3"])
            if row["latest_vs_previous_m3"] is not None
            else None
        )

        has_decline = (
            latest_vs_previous_percent is not None
            and latest_vs_previous_percent <= -decline_percent
        ) or (
            latest_vs_median_percent is not None
            and latest_vs_median_percent <= -decline_percent
        )
        has_abrupt_change = (
            latest_vs_previous_m3 is not None and latest_vs_previous_m3 <= -abrupt_change_m3
        )

        if not (has_decline or has_abrupt_change):
            continue

        anomaly_type = (
            "both"
            if (has_decline and has_abrupt_change)
            else "abrupt"
            if has_abrupt_change
            else "decline"
        )

        # Calculate weight for heatmap: higher weight for larger drops
        weight = 1.0
        if latest_vs_previous_percent is not None:
            weight = max(1.0, min(10.0, abs(latest_vs_previous_percent) / 10.0))

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {
                    "supply": row["supply_code"],
                    "name": row["customer_name"] or "Cliente sin nombre",
                    "district": row["district"] or "SIN DISTRITO",
                    "anomalyType": anomaly_type,
                    "currentVolumeM3": (
                        float(row["current_volume_m3"])
                        if row["current_volume_m3"] is not None
                        else 0
                    ),
                    "previousVolumeM3": (
                        float(row["previous_volume_m3"])
                        if row["previous_volume_m3"] is not None
                        else 0
                    ),
                    "latestDeltaM3": latest_vs_previous_m3,
                    "latestDeltaPercent": latest_vs_previous_percent,
                    "weight": weight,
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}
