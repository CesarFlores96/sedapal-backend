import asyncio
import re

from psycopg_pool import AsyncConnectionPool

from app.sedapalgis.repositories.facturacion import fetch_facturacion
from app.sedapalgis.repositories.shared import fetch_all, fetch_one
from app.sedapalgis.services.consumption_analysis import analyze_supply_consumption, build_monthly_water


# Réplica de los campos derivados que ya calcula el sitio web hermano
# (apps/web/src/modules/reportes/reportes-master.ts y
# infrastructure/repositories/shared.ts) a partir de las mismas columnas de
# bd_facturacion_local, para que los badges del encabezado coincidan.
PAYER_CLASSIFICATION_LABELS = {
    "buen_pagador": "Buen pagador",
    "mal_pagador": "Mal pagador",
    "moroso_critico": "Mal pagador",
    "corte_programado": "Mal pagador",
}


def _classify_segment(segment_name: str | None, office_name: str | None) -> str:
    normalized = f"{segment_name or ''} {office_name or ''}".lower()
    if "fuente propia" in normalized or "fuente_propia" in normalized:
        return "Fuente Propia"
    if "grandes clientes" in normalized or "grandes_clientes" in normalized:
        return "Grandes Clientes"
    return "Sin clasificar"


def _map_payer_classification(value: str | None) -> str:
    if not value:
        return "Regular"
    return PAYER_CLASSIFICATION_LABELS.get(value, "Regular")


async def fetch_report_master_page(
    pool: AsyncConnectionPool,
    *,
    page: int,
    page_size: int,
    search: str,
    filter_active: bool,
    trend_direction: str,
    min_trend_percent: float,
    sort_order: str = "desc",
    baseline_start_period: str,
    baseline_end_period: str,
    target_start_period: str,
    target_end_period: str,
) -> dict:
    """Maestro filtrado por medianas de consumo de agua, igual que la web."""
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    offset = (safe_page - 1) * safe_page_size
    direction = trend_direction if trend_direction in {"increasing", "decreasing", "either"} else "either"
    minimum = max(min_trend_percent, 0)
    order_dir = "ASC" if sort_order.lower() == "asc" else "DESC"

    base_cte = """
        WITH ranges AS (
          SELECT to_date(%s || '-01', 'YYYY-MM-DD') AS baseline_start,
                 to_date(%s || '-01', 'YYYY-MM-DD') AS baseline_end,
                 to_date(%s || '-01', 'YYYY-MM-DD') AS target_start,
                 to_date(%s || '-01', 'YYYY-MM-DD') AS target_end
        ), debt_ranked AS (
          SELECT cd.supply_code, cd.period_year::int AS year, cd.period_month::int AS month,
                 cd.billed_volume_m3::float8 AS volume,
                 row_number() OVER (
                   PARTITION BY cd.supply_code, cd.period_year::int, cd.period_month::int, lower(cd.concept)
                   ORDER BY cd.updated_at DESC NULLS LAST, cd.created_at DESC NULLS LAST, cd.id DESC
                 ) AS source_rank
          FROM public.customer_debts cd, ranges r
          WHERE cd.concept = 'consumo_agua'
            AND make_date(cd.period_year::int, cd.period_month::int, 1)
                BETWEEN r.baseline_start AND r.target_end
        ), daily_ranked AS (
          SELECT b.supply_code, extract(year FROM b.issue_date)::int AS year,
                 extract(month FROM b.issue_date)::int AS month, b.billed_volume_m3::float8 AS volume,
                 row_number() OVER (
                   PARTITION BY b.supply_code, extract(year FROM b.issue_date)::int, extract(month FROM b.issue_date)::int, lower(b.concept)
                   ORDER BY b.source_batch_date DESC NULLS LAST, b.imported_at DESC NULLS LAST,
                            b.source_file DESC NULLS LAST, b.source_line_number DESC NULLS LAST, b.id DESC
                 ) AS source_rank
          FROM public.customer_supply_billing_daily b, ranges r
          WHERE b.issue_date >= r.baseline_start
            AND b.issue_date < r.target_end + interval '1 month'
            AND b.concept = 'consumo_agua'
        ), monthly AS (
          SELECT supply_code, make_date(year, month, 1) AS period, coalesce(volume, 0) AS volume
          FROM debt_ranked WHERE source_rank = 1
          UNION ALL
          SELECT daily.supply_code, make_date(daily.year, daily.month, 1), coalesce(daily.volume, 0)
          FROM daily_ranked daily
          WHERE daily.source_rank = 1
            AND NOT EXISTS (
              SELECT 1 FROM debt_ranked debt
              WHERE debt.source_rank = 1 AND debt.supply_code = daily.supply_code
                AND debt.year = daily.year AND debt.month = daily.month
            )
        ), medians AS (
          SELECT monthly.supply_code,
                 (percentile_cont(0.5) WITHIN GROUP (ORDER BY volume)
                   FILTER (WHERE period BETWEEN r.baseline_start AND r.baseline_end))::float8 AS baseline_median,
                 (percentile_cont(0.5) WITHIN GROUP (ORDER BY volume)
                   FILTER (WHERE period BETWEEN r.target_start AND r.target_end))::float8 AS target_median,
                 count(*) FILTER (WHERE period BETWEEN r.baseline_start AND r.baseline_end)::int AS baseline_points,
                 count(*) FILTER (WHERE period BETWEEN r.target_start AND r.target_end)::int AS target_points,
                 ((extract(year FROM age(r.baseline_end, r.baseline_start)) * 12
                    + extract(month FROM age(r.baseline_end, r.baseline_start)) + 1))::int AS baseline_months,
                 ((extract(year FROM age(r.target_end, r.target_start)) * 12
                    + extract(month FROM age(r.target_end, r.target_start)) + 1))::int AS target_months
          FROM monthly CROSS JOIN ranges r
          GROUP BY monthly.supply_code, r.baseline_start, r.baseline_end, r.target_start, r.target_end
        ), trends AS (
          SELECT supply_code, baseline_median, target_median,
                 CASE
                   WHEN baseline_median = 0 AND target_median = 0 THEN 0
                   WHEN baseline_median = 0 AND target_median > 0 THEN 100
                   ELSE ((target_median - baseline_median) / nullif(baseline_median, 0) * 100)::float8
                 END AS trend_percent,
                 target_median - baseline_median AS delta_m3,
                 baseline_points, target_points, baseline_months, target_months
          FROM medians
        ), debt_by_supply AS (
          SELECT customer_supply_id, sum(total_soles)::float8 AS supply_debt_soles
          FROM public.customer_debts
          WHERE status NOT IN ('pagada', 'condonada')
          GROUP BY customer_supply_id
        ), base AS (
          SELECT cs.supply_code,
                 coalesce(cs.customer_name, c.business_name, c.full_name, 'SUMINISTRO ' || cs.supply_code) AS customer_name,
                 coalesce(cs.district, c.district, 'SIN DISTRITO') AS district,
                 coalesce(debt.supply_debt_soles, 0)::float8 AS debt,
                 cs.office_name, cs.segment AS segment_name, cs.meter_code AS meter_serial,
                 cs.route_code, cs.itinerary_code,
                 %s || ' / ' || %s AS trend_period,
                 trends.target_median AS current_volume,
                 trends.baseline_median AS previous_volume,
                 trends.target_median, trends.baseline_median, trends.trend_percent
          FROM public.customer_supplies cs
          JOIN public.customers c ON c.id = cs.customer_id
          LEFT JOIN debt_by_supply debt ON debt.customer_supply_id = cs.id
          LEFT JOIN trends ON trends.supply_code = cs.supply_code
          WHERE (%s = '' OR cs.supply_code ILIKE '%%' || %s || '%%'
                 OR coalesce(cs.customer_name, c.business_name, c.full_name, '') ILIKE '%%' || %s || '%%'
                 OR coalesce(cs.district, c.district, '') ILIKE '%%' || %s || '%%')
            AND (%s = false OR (
                 trends.baseline_median >= 100
                 AND trends.baseline_points::float8 / nullif(trends.baseline_months, 0) >= 0.5
                 AND trends.target_points::float8 / nullif(trends.target_months, 0) >= 0.5
                 AND abs(trends.delta_m3) >= 50
                 AND abs(trends.trend_percent) >= %s
                 AND (%s = 'either'
                      OR (%s = 'increasing' AND trends.trend_percent > 0)
                      OR (%s = 'decreasing' AND trends.trend_percent < 0))))
        )
    """
    filters = [
        baseline_start_period, baseline_end_period, target_start_period, target_end_period,
        target_start_period, target_end_period,
        search, search, search, search,
        filter_active, minimum, direction, direction, direction,
    ]
    rows = await fetch_all(
        pool,
        base_cte + f"""
          SELECT supply_code AS "supplyCode", customer_name AS "customerName", district, debt,
                 office_name AS "officeName", segment_name AS "segmentName", meter_serial AS "meterSerial",
                 route_code AS "routeCode", itinerary_code AS "itineraryCode", trend_period AS "trendPeriod",
                 current_volume AS "currentVolume", previous_volume AS "previousVolume", trend_percent AS "trendPercent",
                 baseline_median AS "baselineMedianM3", target_median AS "targetMedianM3",
                 count(*) OVER()::int AS "_total",
                 coalesce(sum(debt) OVER(), 0)::float8 AS "_totalDebt",
                 coalesce(sum(CASE WHEN lower(concat_ws(' ', coalesce(segment_name, ''), coalesce(office_name, ''))) LIKE '%%grandes clientes%%' THEN debt ELSE 0 END) OVER(), 0)::float8 AS "_grandesClientesDebt",
                 coalesce(sum(CASE WHEN lower(concat_ws(' ', coalesce(segment_name, ''), coalesce(office_name, ''))) LIKE '%%fuente propia%%' THEN debt ELSE 0 END) OVER(), 0)::float8 AS "_fuentePropiaDebt"
          FROM base
          ORDER BY abs(coalesce(trend_percent, 0)) {order_dir}, customer_name, supply_code
          LIMIT %s OFFSET %s
        """,
        [*filters, safe_page_size, offset],
    )
    summary = rows[0] if rows else {}
    data = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]
    return {
        "data": data,
        "page": safe_page,
        "pageSize": safe_page_size,
        "total": int(summary.get("_total") or 0),
        "summary": {
            "totalDebt": summary.get("_totalDebt") or 0,
            "grandesClientesDebt": summary.get("_grandesClientesDebt") or 0,
            "fuentePropiaDebt": summary.get("_fuentePropiaDebt") or 0,
        },
    }


async def fetch_report_header(pool: AsyncConnectionPool, supply_code: str) -> dict | None:
    row = await fetch_one(
        pool,
        """
        SELECT
          coalesce(cs.customer_name, c.business_name, c.full_name) AS customer_name,
          coalesce(cs.district, c.district) AS district,
          cs.segment AS segment_name,
          cs.office_name,
          c.payer_classification,
          coalesce(debt.supply_debt_soles, 0)::float8 AS debt
        FROM public.customer_supplies cs
        JOIN public.customers c ON c.id = cs.customer_id
        LEFT JOIN LATERAL (
          SELECT sum(cd.total_soles)::float8 AS supply_debt_soles
          FROM public.customer_debts cd
          WHERE cd.customer_supply_id = cs.id
            AND cd.status NOT IN ('pagada', 'condonada')
        ) debt ON true
        WHERE cs.supply_code = %s
        LIMIT 1
        """,
        [supply_code],
    )
    if not row:
        return None
    debt = row.get("debt") or 0.0
    return {
        "customerName": row.get("customer_name"),
        "district": row.get("district"),
        "classification": _classify_segment(row.get("segment_name"), row.get("office_name")),
        "payerClassification": _map_payer_classification(row.get("payer_classification")),
        "serviceStatus": "Pendiente" if debt > 0 else "Activo",
        "debt": debt,
    }


async def build_supply_analysis(pool: AsyncConnectionPool, supply_code: str) -> dict:
    """Análisis estadístico de consumo (MDAS-2.0), calculado enteramente en
    este backend contra bd_facturacion_local -- sin depender de ningún
    servicio externo."""
    billing_rows = await fetch_facturacion(pool, supply_code)
    monthly_water = build_monthly_water(billing_rows)
    analysis = analyze_supply_consumption(supply_code, monthly_water)
    analysis["billingRows"] = billing_rows
    return analysis


async def build_client_lot_analysis(pool: AsyncConnectionPool, supply_codes: list[str]) -> dict:
    """Construye una sola serie temporal para varios NIS del mismo cliente y lote."""
    normalized_codes = list(dict.fromkeys(code.strip() for code in supply_codes if code.strip()))[:50]
    if len(normalized_codes) < 2:
        raise ValueError("El reporte por cliente y lote requiere al menos dos NIS.")

    members = await fetch_all(
        pool,
        """
        SELECT cs.supply_code, cs.customer_name, cs.district,
               CASE
                 WHEN nullif(regexp_replace(coalesce(cs.id_doc_number, ''), '[^[:alnum:]]', '', 'g'), '') IS NOT NULL
                   THEN 'document:' || upper(regexp_replace(cs.id_doc_number, '[^[:alnum:]]', '', 'g'))
                 WHEN nullif(trim(cs.customer_code), '') IS NOT NULL THEN 'customer-code:' || trim(cs.customer_code)
                 WHEN cs.customer_id IS NOT NULL THEN 'customer:' || cs.customer_id::text
                 WHEN nullif(trim(cs.customer_name), '') IS NOT NULL
                   THEN 'name:' || lower(regexp_replace(cs.customer_name, '[^[:alnum:]]', '', 'g'))
                 ELSE 'supply:' || cs.supply_code
               END AS customer_key,
               CASE
                 WHEN nullif(link.cup_code, '') IS NOT NULL THEN 'cup:' || link.cup_code
                 WHEN nullif(cs.lot_code, '') IS NOT NULL
                   THEN 'lot:' || coalesce(cs.district, '') || ':' || cs.lot_code
                 ELSE 'supply:' || cs.supply_code
               END AS property_key
        FROM public.customer_supplies cs
        LEFT JOIN public.gis_supply_lot_links link ON link.supply_id = cs.id
        WHERE cs.supply_code = ANY(%s::text[])
        ORDER BY cs.supply_code
        """,
        [normalized_codes],
    )
    if len(members) != len(normalized_codes):
        raise ValueError("Uno o más NIS del grupo no existen.")
    customer_keys = {row["customer_key"] for row in members}
    property_keys = {row["property_key"] for row in members}
    if len(customer_keys) != 1 or len(property_keys) != 1:
        raise ValueError("Los NIS no pertenecen al mismo cliente y lote.")

    headers, billing_sets, details_sets = await asyncio.gather(
        asyncio.gather(*(fetch_report_header(pool, code) for code in normalized_codes)),
        asyncio.gather(*(fetch_facturacion(pool, code) for code in normalized_codes)),
        asyncio.gather(*(fetch_supply_details(pool, code) for code in normalized_codes)),
    )
    grouped: dict[tuple[int, int, str], dict] = {}
    for rows in billing_sets:
        for row in rows:
            year = int(row["period_year"])
            month = int(row["period_month"])
            concept = str(row.get("concept") or "")
            key = (year, month, concept.lower())
            current = grouped.setdefault(key, {
                "period_year": year,
                "period_month": month,
                "concept": concept,
                "billed_volume_m3": 0.0,
                "amount_soles": 0.0,
            })
            current["billed_volume_m3"] += float(row.get("billed_volume_m3") or 0)
            current["amount_soles"] += float(row.get("amount_soles") or 0)

    property_code = next(iter(property_keys))
    analysis = analyze_supply_consumption(property_code, build_monthly_water(list(grouped.values())))

    # Desglose por NIS para las barras apiladas del gráfico consolidado: cada
    # fila mensual conserva el volumen individual de cada suministro del grupo,
    # ademas del total agregado que ya calcula analyze_supply_consumption.
    per_supply_monthly = {
        code: build_monthly_water(rows)
        for code, rows in zip(normalized_codes, billing_sets)
    }
    for year_key, year_data in analysis["analysisByYear"].items():
        year_int = int(year_key)
        for row in year_data["evolutionRows"]:
            month = row["month"]
            row["bySupply"] = {
                code: per_supply_monthly.get(code, {}).get(year_int, {}).get(month)
                for code in normalized_codes
            }

    analysis["details"] = _merge_supply_details(normalized_codes, details_sets)
    analysis["details"]["billing"] = list(grouped.values())

    valid_headers = [header for header in headers if header]
    classifications = {header["classification"] for header in valid_headers}
    total_debt = sum(float(header.get("debt") or 0) for header in valid_headers)
    analysis["header"] = {
        "customerName": members[0].get("customer_name"),
        "district": members[0].get("district"),
        "classification": "Grandes Clientes" if "Grandes Clientes" in classifications
            else "Fuente Propia" if "Fuente Propia" in classifications else "Sin clasificar",
        "payerClassification": "Mal pagador" if any(
            header.get("payerClassification") == "Mal pagador" for header in valid_headers
        ) else "Regular",
        "serviceStatus": "Pendiente" if total_debt > 0 else "Activo",
        "debt": total_debt,
    }
    analysis["group"] = {
        "analysisScope": "property",
        "propertyCode": property_code,
        "supplyCodes": normalized_codes,
        "supplyCount": len(normalized_codes),
    }
    return analysis


_DETAIL_DATE_FIELDS = {
    "stateReadings": "readingDate",
    "meterInstallations": "installationDate",
    "workOrders": "completedAt",
    "anomalies": "detectedAt",
    "inspections": "visitDate",
}


def _merge_supply_details(codes: list[str], details_sets: list[dict]) -> dict:
    """Combina el detalle operativo (lecturas, medidores, OT, anomalias,
    inspecciones) de cada NIS del grupo en una sola estructura, etiquetando
    cada registro con su NIS de origen para que la UI pueda explicar a cual
    suministro del lote corresponde cada hallazgo."""
    merged: dict[str, list[dict]] = {key: [] for key in _DETAIL_DATE_FIELDS}
    for code, details in zip(codes, details_sets):
        for key in merged:
            for item in details.get(key, []):
                merged[key].append({**item, "supplyCode": code})
    for key, date_field in _DETAIL_DATE_FIELDS.items():
        merged[key].sort(key=lambda item, field=date_field: item.get(field) or "", reverse=True)
        merged[key] = merged[key][:200]
    return merged


async def fetch_supply_details(pool: AsyncConnectionPool, supply_code: str) -> dict:
    state_readings = await fetch_all(
        pool,
        """
        SELECT reading_date::text AS "readingDate", reading_type AS "readingType",
               meter_type AS "meterType", meter_serial AS "meterSerial", diameter_mm AS "diameterMm",
               reading_value AS "readingValue", incidence_label AS "incidenceLabel",
               incidence_detail AS "incidenceDetail", observation
        FROM public.customer_supply_state_readings
        WHERE supply_code = %s
        ORDER BY reading_date DESC NULLS LAST, id DESC
        LIMIT 100
        """,
        [supply_code],
    )
    meter_installations = await fetch_all(
        pool,
        """
        SELECT installation_date::text AS "installationDate", process_date::text AS "processDate",
               meter_serial AS "meterSerial", previous_meter_serial AS "previousMeterSerial",
               diameter_mm AS "diameterMm", status_code AS status,
               work_order_number::text AS "workOrderNumber",
               service_order_number::text AS "serviceOrderNumber",
               current_reading::float8 AS "currentReading", previous_reading::float8 AS "previousReading",
               useful_life_observation AS observation
        FROM public.meter_park_snapshots
        WHERE supply_code = %s
        ORDER BY installation_date DESC NULLS LAST, process_date DESC NULLS LAST
        LIMIT 100
        """,
        [supply_code],
    )
    work_orders = await fetch_all(
        pool,
        """
        SELECT wo.code, wo.order_type::text AS "orderType", wo.status::text AS status,
               wo.priority::text AS priority, wo.scheduled_date::text AS "scheduledDate",
               wo.completed_at::text AS "completedAt", wo.title, wo.description, wo.result_notes AS "resultNotes"
        FROM public.work_orders wo
        LEFT JOIN public.meters m ON m.id = wo.meter_id
        LEFT JOIN public.domestic_connections dc ON dc.id = coalesce(wo.connection_id, m.connection_id)
        WHERE dc.supply_code_ref = %s OR EXISTS (
          SELECT 1 FROM public.anomalies a WHERE a.work_order_id = wo.id AND a.supply_code = %s
        )
        ORDER BY wo.scheduled_date DESC NULLS LAST, wo.code
        LIMIT 100
        """,
        [supply_code, supply_code],
    )
    anomalies = await fetch_all(
        pool,
        """
        SELECT anomaly_type AS "anomalyType", detected_at::text AS "detectedAt",
               detected_value::float8 AS "detectedValue", expected_value::float8 AS "expectedValue",
               deviation_pct::float8 AS "deviationPercent", resolved,
               resolved_at::text AS "resolvedAt", resolution_notes AS "resolutionNotes",
               anomaly_status AS status, reading_observation AS "readingObservation",
               billing_observation AS "billingObservation", inspection_observation AS "inspectionObservation"
        FROM public.anomalies
        WHERE supply_code = %s
        ORDER BY detected_at DESC NULLS LAST
        LIMIT 100
        """,
        [supply_code],
    )
    inspections = await fetch_all(
        pool,
        """
        SELECT inspection_date::text AS "inspectionDate", visit_date::text AS "visitDate",
               work_order_number AS "workOrderNumber", inspection_typology AS typology,
               inspection_result AS result, service_status AS "serviceStatus",
               meter_serial AS "meterSerial", reading_value AS "readingValue", observation
        FROM public.commercial_inspections
        WHERE supply_code = %s
        ORDER BY coalesce(visit_date, inspection_date::date) DESC NULLS LAST, id DESC
        LIMIT 100
        """,
        [supply_code],
    )
    details = {
        "stateReadings": state_readings,
        "meterInstallations": meter_installations,
        "workOrders": work_orders,
        "anomalies": anomalies,
        "inspections": inspections,
    }
    labels = await _load_catalog_labels(pool, _collect_code_candidates(details))
    return _decode_details_codes(details, labels)


_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{2,3}\d{2,4}\b")

_DETAIL_CODE_FIELDS: dict[str, tuple[str, ...]] = {
    "stateReadings": ("readingType", "incidenceLabel", "incidenceDetail", "observation"),
    "meterInstallations": ("status", "observation"),
    "workOrders": ("orderType", "status", "priority", "resultNotes", "description"),
    "anomalies": (
        "anomalyType", "status", "readingObservation", "billingObservation",
        "inspectionObservation", "resolutionNotes",
    ),
    "inspections": ("typology", "result", "serviceStatus", "observation"),
}


def _collect_code_candidates(details: dict) -> set[str]:
    """Extrae tokens con forma de codigo de catalogo (p.ej. IE003, TO153) de los
    campos de texto libre/estado devueltos por fetch_supply_details, para
    resolverlos en un solo viaje a supervision_code_catalog en vez de adivinar
    su significado."""
    candidates: set[str] = set()
    for key, fields in _DETAIL_CODE_FIELDS.items():
        for row in details.get(key, []):
            for field in fields:
                value = row.get(field)
                if value:
                    candidates.update(_CODE_TOKEN_RE.findall(str(value)))
    return candidates


async def _load_catalog_labels(pool: AsyncConnectionPool, codes: set[str]) -> dict[str, str]:
    """Resuelve codigos contra el catalogo real (public.supervision_code_catalog,
    la misma tabla que ya usa gis.py para CUA). Un codigo sin fila en el
    catalogo se deja tal cual -- nunca se fabrica una traduccion."""
    if not codes:
        return {}
    rows = await fetch_all(
        pool,
        """
        SELECT DISTINCT ON (code) code, description
        FROM public.supervision_code_catalog
        WHERE code = ANY(%s::text[]) AND nullif(trim(description), '') IS NOT NULL
        ORDER BY code, catalog_number
        """,
        [list(codes)],
    )
    return {row["code"]: row["description"].strip() for row in rows}


def _decode_value(value: str | None, labels: dict[str, str]) -> str | None:
    if not value or not labels:
        return value

    def _sub(match: "re.Match[str]") -> str:
        code = match.group(0)
        label = labels.get(code)
        return f"{label} ({code})" if label else code

    return _CODE_TOKEN_RE.sub(_sub, value)


def _decode_details_codes(details: dict, labels: dict[str, str]) -> dict:
    if not labels:
        return details
    decoded: dict[str, list[dict]] = {}
    for key, fields in _DETAIL_CODE_FIELDS.items():
        decoded[key] = [
            {**row, **{field: _decode_value(row.get(field), labels) for field in fields}}
            for row in details.get(key, [])
        ]
    return decoded


async def fetch_supply_indicators(pool: AsyncConnectionPool, supply_code: str) -> dict:
    """Indicadores trazables calculados sobre facturacion canonica y GIS real.

    Las comparaciones usan el ultimo periodo de agua disponible del suministro.
    Los indicadores que requieren lote solo se publican cuando el punto cae dentro
    de una geometria catastral; no se aproximan areas ni perimetros.
    """
    territorial = await fetch_one(
        pool,
        """
        WITH target AS (
          SELECT cs.supply_code, gsl.geom, cst.district_code,
                 lot.id AS lot_id, lot.lot_code, lot.levels,
                 ST_Area(lot.geom::geography)::float8 AS lot_area_m2,
                 ST_Perimeter(lot.geom::geography)::float8 AS lot_perimeter_m,
                 coalesce(lot.block_id, point_block.id) AS block_id,
                 coalesce(lot_block.block_code, point_block.block_code) AS block_code,
                 coalesce(lot_block.geom, point_block.geom) AS block_geom,
                 ST_Perimeter(coalesce(lot_block.geom, point_block.geom)::geography)::float8 AS block_perimeter_m
          FROM public.customer_supplies cs
          LEFT JOIN public.gis_supply_locations gsl ON gsl.supply_code = cs.supply_code
          LEFT JOIN public.customer_supplies_territory cst ON cst.supply_code = cs.supply_code
          LEFT JOIN LATERAL (
            SELECT l.* FROM public.gis_lots l
            WHERE gsl.geom IS NOT NULL
              AND l.geom && ST_Expand(gsl.geom, 0.00002)
              AND ST_DWithin(l.geom::geography, gsl.geom::geography, 1)
            ORDER BY CASE WHEN ST_Covers(l.geom, gsl.geom) THEN 0 ELSE 1 END,
                     ST_Distance(l.geom::geography, gsl.geom::geography), ST_Area(l.geom)
            LIMIT 1
          ) lot ON true
          LEFT JOIN public.gis_blocks lot_block ON lot_block.id = lot.block_id
          LEFT JOIN LATERAL (
            SELECT b.* FROM public.gis_blocks b
            WHERE gsl.geom IS NOT NULL
              AND b.geom && ST_Expand(gsl.geom, 0.00002)
              AND ST_DWithin(b.geom::geography, gsl.geom::geography, 1)
            ORDER BY CASE WHEN ST_Covers(b.geom, gsl.geom) THEN 0 ELSE 1 END,
                     ST_Distance(b.geom::geography, gsl.geom::geography), ST_Area(b.geom)
            LIMIT 1
          ) point_block ON true
          WHERE cs.supply_code = %s
          LIMIT 1
        ), target_period AS (
          SELECT period_year, period_month FROM (
            SELECT cd.period_year::int AS period_year, cd.period_month::int AS period_month
            FROM public.customer_debts cd
            WHERE cd.supply_code = %s AND lower(cd.concept) = 'consumo_agua'
            UNION ALL
            SELECT extract(year FROM b.issue_date)::int, extract(month FROM b.issue_date)::int
            FROM public.customer_supply_billing_daily b
            WHERE b.supply_code = %s AND b.issue_date IS NOT NULL
              AND lower(b.concept) = 'consumo_agua'
          ) periods
          ORDER BY period_year DESC, period_month DESC LIMIT 1
        ), debt_ranked AS (
          SELECT cd.supply_code, lower(cd.concept) AS concept,
                 cd.billed_volume_m3::float8 AS volume, cd.amount_soles::float8 AS amount,
                 row_number() OVER (
                   PARTITION BY cd.supply_code, lower(cd.concept)
                   ORDER BY cd.updated_at DESC NULLS LAST, cd.created_at DESC NULLS LAST, cd.id DESC
                 ) AS source_rank
          FROM public.customer_debts cd, target_period period
          WHERE cd.period_year = period.period_year AND cd.period_month = period.period_month
        ), daily_ranked AS (
          SELECT b.supply_code, lower(b.concept) AS concept,
                 b.billed_volume_m3::float8 AS volume, b.amount_soles::float8 AS amount,
                 row_number() OVER (
                   PARTITION BY b.supply_code, lower(b.concept)
                   ORDER BY b.source_batch_date DESC NULLS LAST, b.imported_at DESC NULLS LAST,
                            b.source_file DESC NULLS LAST, b.source_line_number DESC NULLS LAST, b.id DESC
                 ) AS source_rank
          FROM public.customer_supply_billing_daily b, target_period period
          WHERE b.issue_date >= make_date(period.period_year, period.period_month, 1)
            AND b.issue_date < make_date(period.period_year, period.period_month, 1) + interval '1 month'
        ), canonical_rows AS (
          SELECT supply_code, concept, volume, amount FROM debt_ranked WHERE source_rank = 1
          UNION ALL
          SELECT daily.supply_code, daily.concept, daily.volume, daily.amount
          FROM daily_ranked daily
          WHERE daily.source_rank = 1 AND NOT EXISTS (
            SELECT 1 FROM debt_ranked debt
            WHERE debt.source_rank = 1 AND debt.supply_code = daily.supply_code
              AND debt.concept = daily.concept
          )
        ), current_values AS (
          SELECT supply_code,
                 max(volume) FILTER (WHERE concept = 'consumo_agua')::float8 AS volume,
                 sum(coalesce(amount, 0))::float8 AS billed_amount
          FROM canonical_rows GROUP BY supply_code
        ), district_peers AS (
          SELECT peer.supply_code, values.volume, values.billed_amount
          FROM target
          JOIN public.customer_supplies_territory peer ON peer.district_code = target.district_code
          JOIN current_values values ON values.supply_code = peer.supply_code
          WHERE values.volume IS NOT NULL
        ), lot_peers AS (
          -- 1. Suministros encontrados por proximidad espacial al lote catastral
          SELECT DISTINCT locations.supply_code, values.volume, values.billed_amount, locations.geom
          FROM target
          JOIN public.gis_lots current_lot ON current_lot.id = target.lot_id
          JOIN public.gis_supply_locations locations
            ON locations.geom && ST_Expand(current_lot.geom, 0.00002)
           AND ST_DWithin(current_lot.geom::geography, locations.geom::geography, 1)
          JOIN current_values values ON values.supply_code = locations.supply_code
          WHERE values.volume IS NOT NULL
          -- 2. Suministros vinculados al mismo lote por cup_code; usa centroide del lote si no tiene GPS
          UNION
          SELECT DISTINCT peer_link.supply_code, cv.volume, cv.billed_amount,
                 coalesce(loc.geom, ST_PointOnSurface(current_lot.geom))
          FROM target
          JOIN public.gis_lots current_lot ON current_lot.id = target.lot_id
          JOIN public.gis_supply_lot_links peer_link ON peer_link.cup_code = current_lot.cup_code
          JOIN current_values cv ON cv.supply_code = peer_link.supply_code
          LEFT JOIN public.gis_supply_locations loc ON loc.supply_code = peer_link.supply_code
          WHERE cv.volume IS NOT NULL AND current_lot.cup_code IS NOT NULL
          -- 3. Siempre incluir el suministro actual; centroide del lote si no tiene GPS propio
          UNION
          SELECT target.supply_code, cv.volume, cv.billed_amount,
                 coalesce(target.geom, ST_PointOnSurface(current_lot.geom))
          FROM target
          LEFT JOIN public.gis_lots current_lot ON current_lot.id = target.lot_id
          LEFT JOIN current_values cv ON cv.supply_code = target.supply_code
          WHERE target.geom IS NOT NULL OR current_lot.geom IS NOT NULL
        ), block_peers AS (
          SELECT DISTINCT locations.supply_code, values.volume, values.billed_amount
          FROM target
          JOIN public.gis_lots block_lot ON block_lot.block_id = target.block_id
          JOIN public.gis_supply_locations locations
            ON locations.geom && ST_Expand(block_lot.geom, 0.00002)
           AND ST_DWithin(block_lot.geom::geography, locations.geom::geography, 1)
          JOIN current_values values ON values.supply_code = locations.supply_code
          WHERE values.volume IS NOT NULL
        ), block_supplies_geo AS (
          -- Suministros de la manzana con sus coordenadas GPS para mostrar en el mapa
          SELECT DISTINCT locations.supply_code, values.volume, values.billed_amount, locations.geom
          FROM target
          JOIN public.gis_lots block_lot ON block_lot.block_id = target.block_id
          JOIN public.gis_supply_locations locations
            ON locations.geom && ST_Expand(block_lot.geom, 0.00002)
           AND ST_DWithin(block_lot.geom::geography, locations.geom::geography, 1)
          JOIN current_values values ON values.supply_code = locations.supply_code
          WHERE values.volume IS NOT NULL AND target.block_id IS NOT NULL
          -- Incluir también los suministros del lote (con sus puntos/centroides)
          UNION
          SELECT lp.supply_code, lp.volume, lp.billed_amount, lp.geom
          FROM lot_peers lp WHERE lp.geom IS NOT NULL
        ), neighbor_peers AS (
          SELECT locations.supply_code, values.volume
          FROM target
          JOIN public.gis_supply_locations locations
            ON target.geom IS NOT NULL
           AND locations.geom && ST_Expand(target.geom, 0.003)
           AND ST_DWithin(locations.geom::geography, target.geom::geography, 250)
          JOIN current_values values ON values.supply_code = locations.supply_code
          WHERE values.volume IS NOT NULL AND locations.supply_code <> target.supply_code
        ), current_link AS (
          SELECT link.cua_catalog_id, link.cup_code
          FROM public.gis_supply_lot_links link, target
          WHERE link.supply_code = target.supply_code
        -- Solo calcular áreas de CUP usados por el distrito o la misma actividad.
        -- Evita recorrer todo el catastro nacional para un único suministro.
        ), candidate_cups AS (
          SELECT peer_link.cup_code
          FROM target
          JOIN public.customer_supplies_territory peer_territory
            ON peer_territory.district_code = target.district_code
          JOIN public.gis_supply_lot_links peer_link
            ON peer_link.supply_code = peer_territory.supply_code
          WHERE peer_link.cup_code IS NOT NULL
          UNION
          SELECT peer_link.cup_code
          FROM current_link
          JOIN public.gis_supply_lot_links peer_link
            ON peer_link.cua_catalog_id = current_link.cua_catalog_id
          WHERE current_link.cua_catalog_id IS NOT NULL
            AND peer_link.cup_code IS NOT NULL
        -- Área agregada por cup_code: un lote lógico puede tener varios polígonos.
        ), lot_area_by_cup AS (
          SELECT lot.cup_code, sum(ST_Area(lot.geom::geography))::float8 AS area_m2
          FROM candidate_cups candidate
          JOIN public.gis_lots lot ON lot.cup_code = candidate.cup_code
          GROUP BY lot.cup_code
        ), lot_shapes_by_cup AS (
          SELECT lot.cup_code,
                 ST_Centroid(ST_Collect(lot.geom)) AS point_geom,
                 ST_AsGeoJSON(ST_Union(lot.geom))::jsonb AS lot_geometry,
                 ST_AsGeoJSON(ST_Union(block.geom))::jsonb AS block_geometry
          FROM candidate_cups candidate
          JOIN public.gis_lots lot ON lot.cup_code = candidate.cup_code
          LEFT JOIN public.gis_blocks block ON block.id = lot.block_id
          GROUP BY lot.cup_code
        ), district_lot_peers AS (
          SELECT peer_link.supply_code, values.volume, area_by_cup.area_m2
          FROM target
          JOIN public.customer_supplies_territory peer_territory ON peer_territory.district_code = target.district_code
          JOIN public.gis_supply_lot_links peer_link ON peer_link.supply_code = peer_territory.supply_code
          JOIN lot_area_by_cup area_by_cup ON area_by_cup.cup_code = peer_link.cup_code
          JOIN current_values values ON values.supply_code = peer_link.supply_code
          WHERE area_by_cup.area_m2 > 0 AND values.volume IS NOT NULL
        -- Lotes "similares": misma clasificacion de uso de agua (CUA) y area
        -- dentro de +/-30 pct de la del lote actual, sin importar distrito.
        ), similar_lot_peers AS (
          SELECT peer_link.supply_code, values.volume, area_by_cup.area_m2, peer_link.cua_catalog_id, peer_link.cup_code
          FROM target
          CROSS JOIN current_link
          JOIN public.gis_supply_lot_links peer_link
            ON peer_link.cua_catalog_id = current_link.cua_catalog_id
           AND peer_link.supply_code <> target.supply_code
          JOIN lot_area_by_cup area_by_cup ON area_by_cup.cup_code = peer_link.cup_code
          JOIN current_values values ON values.supply_code = peer_link.supply_code
          WHERE current_link.cua_catalog_id IS NOT NULL
            AND target.lot_area_m2 IS NOT NULL
            AND values.volume IS NOT NULL
            AND area_by_cup.area_m2 BETWEEN target.lot_area_m2 * 0.7 AND target.lot_area_m2 * 1.3
        )
        SELECT target.district_code AS "districtCode", target.geom IS NOT NULL AS "hasGeolocation",
               target.lot_id IS NOT NULL AS "hasLot", target.block_id IS NOT NULL AS "hasBlock",
               target.block_code AS "blockCode",
               ST_AsGeoJSON(target.block_geom)::jsonb AS "blockGeometry",
               coalesce((
                 SELECT jsonb_agg(jsonb_build_object(
                   'id', block_lot.id::text,
                   'lotCode', block_lot.lot_code,
                   'areaM2', ST_Area(block_lot.geom::geography)::float8,
                   'isCurrent', block_lot.id = target.lot_id,
                   'geometry', ST_AsGeoJSON(block_lot.geom)::jsonb
                 ) ORDER BY block_lot.lot_code)
                 FROM public.gis_lots block_lot
                 WHERE block_lot.block_id = target.block_id
               ), '[]'::jsonb) AS "blockLots",
               target.lot_area_m2 AS "lotAreaM2", target.lot_perimeter_m AS "lotPerimeterM",
               target.block_perimeter_m AS "blockPerimeterM",
               (SELECT sum(ST_Area(block_lot.geom::geography))::float8
                  FROM public.gis_lots block_lot WHERE block_lot.block_id = target.block_id) AS "blockLotAreaM2",
               target.levels AS "lotLevels", period.period_year AS "periodYear",
               period.period_month AS "periodMonth", current.volume AS "currentConsumptionM3",
               current.billed_amount AS "currentBillingSoles",
               (SELECT avg(volume)::float8 FROM district_peers) AS "districtAverageM3",
               (SELECT sum(volume)::float8 FROM district_peers) AS "districtConsumptionM3",
               (SELECT sum(billed_amount)::float8 FROM district_peers) AS "districtBillingSoles",
               (SELECT count(*)::int FROM district_peers) AS "districtSupplyCount",
               CASE WHEN current.volume IS NULL THEN NULL ELSE
                 (SELECT count(*)::int + 1 FROM district_peers WHERE volume > current.volume)
               END AS "districtRank",
               (SELECT count(*)::float8 * 100 / nullif((SELECT count(*) FROM district_peers), 0)
                  FROM district_peers WHERE volume <= current.volume) AS "consumptionPercentile",
               (SELECT avg(volume)::float8 FROM block_peers) AS "blockAverageM3",
               (SELECT sum(volume)::float8 FROM lot_peers) AS "lotConsumptionM3",
               (SELECT count(*)::int FROM lot_peers) AS "lotSupplyCount",
               (SELECT jsonb_agg(jsonb_build_object(
                  'supplyCode', lot_peers.supply_code,
                  'volume', lot_peers.volume,
                  'billingSoles', lot_peers.billed_amount,
                  'isCurrent', lot_peers.supply_code = target.supply_code,
                  'point', ST_AsGeoJSON(lot_peers.geom)::jsonb
                ) ORDER BY lot_peers.volume DESC NULLS LAST)
                FROM lot_peers) AS "lotSupplies",
               (SELECT jsonb_agg(jsonb_build_object(
                   'supplyCode', bsg.supply_code,
                   'volume', bsg.volume,
                   'isCurrent', bsg.supply_code = target.supply_code,
                   'isLotSupply', EXISTS (
                     SELECT 1 FROM lot_peers lp WHERE lp.supply_code = bsg.supply_code
                   ),
                   'point', ST_AsGeoJSON(bsg.geom)::jsonb
                 ) ORDER BY bsg.volume DESC NULLS LAST)
                 FROM block_supplies_geo bsg) AS "blockSupplies",
               (SELECT sum(volume)::float8 FROM block_peers) AS "blockConsumptionM3",
               (SELECT sum(billed_amount)::float8 FROM block_peers) AS "blockBillingSoles",
               (SELECT count(*)::int FROM block_peers) AS "blockSupplyCount",
               CASE WHEN target.block_id IS NULL OR current.volume IS NULL THEN NULL ELSE
                 (SELECT count(*)::int + 1 FROM block_peers WHERE volume > current.volume)
               END AS "blockRank",
               (SELECT avg(volume)::float8 FROM neighbor_peers) AS "neighborAverageM3",
               (SELECT count(*)::int FROM neighbor_peers) AS "neighborCount",
               CASE WHEN target.lot_area_m2 IS NULL OR target.lot_area_m2 <= 0 OR current.volume IS NULL THEN NULL ELSE
                 (SELECT count(*)::int + 1 FROM district_lot_peers
                    WHERE volume / area_m2 > current.volume / target.lot_area_m2)
               END AS "districtPerAreaRank",
               (SELECT count(*)::int FROM district_lot_peers) AS "districtPerAreaSupplyCount",
               coalesce((SELECT jsonb_agg(jsonb_build_object(
                 'supplyCode', peer.supply_code,
                 'volume', peer.volume,
                 'areaM2', peer.area_m2,
                 'customerName', peer.customer_name
               ))
               FROM (
                 SELECT s.supply_code, s.volume, s.area_m2,
                        coalesce(cs.customer_name, c.business_name, c.full_name) AS customer_name
                 FROM district_lot_peers s
                 LEFT JOIN public.customer_supplies cs ON cs.supply_code = s.supply_code
                 LEFT JOIN public.customers c ON c.id = cs.customer_id
                 ORDER BY s.volume / nullif(s.area_m2, 0) DESC NULLS LAST
                 LIMIT 50
               ) peer), '[]'::jsonb) AS "districtLotPeers",
               (SELECT avg(volume)::float8 FROM similar_lot_peers) AS "similarLotsAverageM3",
               (SELECT count(*)::int FROM similar_lot_peers) AS "similarLotsCount",
               coalesce((SELECT jsonb_agg(jsonb_build_object(
                 'supplyCode', peer.supply_code,
                 'volume', peer.volume,
                 'areaM2', peer.area_m2,
                 'cua', peer.cua,
                 'lotGeometry', peer.lot_geometry,
                 'blockGeometry', peer.block_geometry,
                 'point', peer.point,
                 'customerName', peer.customer_name
               ))
               FROM (
                 SELECT s.supply_code, s.volume, s.area_m2,
                        coalesce(nullif(trim(cua_cat.description), ''), cua_cat.code, '') AS cua,
                        cup_shape.lot_geometry,
                        cup_shape.block_geometry,
                        ST_AsGeoJSON(coalesce(loc.geom, cup_shape.point_geom))::jsonb AS point,
                        coalesce(cs.customer_name, c.business_name, c.full_name) AS customer_name
                 FROM similar_lot_peers s
                 LEFT JOIN public.customer_supplies cs ON cs.supply_code = s.supply_code
                 LEFT JOIN public.customers c ON c.id = cs.customer_id
                 LEFT JOIN public.supervision_code_catalog cua_cat ON cua_cat.id = s.cua_catalog_id
                 LEFT JOIN public.gis_supply_locations loc ON loc.supply_code = s.supply_code
                 LEFT JOIN lot_shapes_by_cup cup_shape ON cup_shape.cup_code = s.cup_code
                 ORDER BY s.volume DESC NULLS LAST
                 LIMIT 50
               ) peer), '[]'::jsonb) AS "similarLots"
        FROM target
        LEFT JOIN target_period period ON true
        LEFT JOIN current_values current ON current.supply_code = target.supply_code
        """,
        [supply_code, supply_code, supply_code],
    )
    economic = await fetch_one(
        pool,
        """
        WITH debt_ranked AS (
          SELECT cd.period_year::int AS year, cd.period_month::int AS month,
                 lower(cd.concept) AS concept, cd.amount_soles::float8 AS amount,
                 row_number() OVER (
                   PARTITION BY cd.period_year, cd.period_month, lower(cd.concept)
                   ORDER BY cd.updated_at DESC NULLS LAST, cd.created_at DESC NULLS LAST, cd.id DESC
                 ) AS source_rank
          FROM public.customer_debts cd WHERE cd.supply_code = %s
        ), daily_ranked AS (
          SELECT extract(year FROM b.issue_date)::int AS year,
                 extract(month FROM b.issue_date)::int AS month, lower(b.concept) AS concept,
                 b.amount_soles::float8 AS amount,
                 row_number() OVER (
                   PARTITION BY extract(year FROM b.issue_date), extract(month FROM b.issue_date), lower(b.concept)
                   ORDER BY b.source_batch_date DESC NULLS LAST, b.imported_at DESC NULLS LAST,
                            b.source_file DESC NULLS LAST, b.source_line_number DESC NULLS LAST, b.id DESC
                 ) AS source_rank
          FROM public.customer_supply_billing_daily b
          WHERE b.supply_code = %s AND b.issue_date IS NOT NULL
        ), canonical AS (
          SELECT year, month, concept, amount FROM debt_ranked WHERE source_rank = 1
          UNION ALL
          SELECT daily.year, daily.month, daily.concept, daily.amount FROM daily_ranked daily
          WHERE daily.source_rank = 1 AND NOT EXISTS (
            SELECT 1 FROM debt_ranked debt WHERE debt.source_rank = 1
              AND debt.year = daily.year AND debt.month = daily.month AND debt.concept = daily.concept
          )
        ), monthly AS (
          SELECT year, month, sum(coalesce(amount, 0))::float8 AS amount
          FROM canonical GROUP BY year, month
        ), latest AS (
          SELECT * FROM monthly ORDER BY year DESC, month DESC LIMIT 1
        )
        SELECT latest.year AS "latestYear", latest.month AS "latestMonth",
               latest.amount AS "monthlyBillingSoles",
               (SELECT sum(amount)::float8 FROM monthly WHERE year = latest.year) AS "annualBillingSoles",
               (SELECT avg(amount)::float8 FROM (SELECT amount FROM monthly ORDER BY year DESC, month DESC LIMIT 12) recent) AS "averageTicketSoles",
               (SELECT count(*)::int FROM monthly) AS "billedPeriodCount"
        FROM latest
        """,
        [supply_code, supply_code],
    )
    operations = await fetch_one(
        pool,
        """
        SELECT
          (SELECT count(*)::int FROM public.commercial_inspections i WHERE i.supply_code = %s) AS "inspectionCount",
          (SELECT max(i.inspection_date) FROM public.commercial_inspections i WHERE i.supply_code = %s) AS "lastInspectionAt",
          (SELECT count(*)::int FROM public.anomalies a WHERE a.supply_code = %s AND NOT coalesce(a.resolved, false)) AS "openAnomalyCount",
          (SELECT count(*)::int FROM public.meter_contrastations m WHERE m.supply_code = %s) AS "contrastationCount",
          (SELECT m.result FROM public.meter_contrastations m WHERE m.supply_code = %s ORDER BY m.test_date DESC NULLS LAST LIMIT 1) AS "lastContrastationResult"
        """,
        [supply_code, supply_code, supply_code, supply_code, supply_code],
    )
    spatial = territorial or {}
    current = spatial.get("currentConsumptionM3")
    lot_consumption = spatial.get("lotConsumptionM3")
    block_consumption = spatial.get("blockConsumptionM3")
    area = spatial.get("lotAreaM2")
    perimeter = spatial.get("lotPerimeterM")
    block_area = spatial.get("blockLotAreaM2")
    block_perimeter = spatial.get("blockPerimeterM")
    neighbor_average = spatial.get("neighborAverageM3")
    per_area = lot_consumption / area if lot_consumption is not None and area and area > 0 else None
    per_perimeter = lot_consumption / perimeter if lot_consumption is not None and perimeter and perimeter > 0 else None
    current_per_area = current / area if current is not None and area and area > 0 else None
    current_per_perimeter = current / perimeter if current is not None and perimeter and perimeter > 0 else None
    block_density = block_consumption / block_area if block_consumption is not None and block_area and block_area > 0 else None
    block_per_linear_meter = block_consumption / block_perimeter if block_consumption is not None and block_perimeter and block_perimeter > 0 else None
    neighbor_deviation = (
        (current - neighbor_average) / neighbor_average * 100
        if current is not None and neighbor_average and neighbor_average > 0 else None
    )
    return {
        "coverage": {
            "billing": bool(economic),
            "district": bool(spatial.get("districtCode")),
            "geolocation": bool(spatial.get("hasGeolocation")),
            "block": bool(spatial.get("hasBlock")),
            "lot": bool(spatial.get("hasLot")),
            "operations": bool((operations or {}).get("inspectionCount") or (operations or {}).get("openAnomalyCount")),
        },
        "spatial": {
            **spatial,
            "consumptionPerM2": per_area,
            "consumptionPerLinearMeter": per_perimeter,
            "currentSupplyConsumptionPerM2": current_per_area,
            "currentSupplyConsumptionPerLinearMeter": current_per_perimeter,
            "blockConsumptionDensityM3PerM2": block_density,
            "blockConsumptionPerLinearMeter": block_per_linear_meter,
            "neighborDeviationPercent": neighbor_deviation,
        },
        "economic": economic or {},
        "operations": operations or {},
    }


async def fetch_abrupt_consumption_drops(
    pool: AsyncConnectionPool,
    *,
    page: int = 1,
    page_size: int = 10,
    classification: str | None = None,
    kind: str | None = None,
    district: str = "",
    analysis_scope: str = "supply",
    search: str = "",
    vector_supply_codes: list[str] | None = None,
) -> dict:
    """Devuelve suministros cuyo consumo mensual cae a cero o a <=15% de su
    referencia inmediata. El cálculo se hace en PostgreSQL para no transferir
    toda la facturación al visor."""
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    offset = (safe_page - 1) * safe_page_size
    normalized_search = search.strip()[:160]
    normalized_district = district.strip()[:100]
    safe_analysis_scope = "property" if analysis_scope == "property" else "supply"
    search_pattern = f"%{normalized_search}%"
    text_codes: list[str] = []
    if normalized_search:
        text_rows = await fetch_all(
            pool,
            """
            SELECT supply_code
            FROM public.customer_supplies
            WHERE supply_code ILIKE %s
               OR coalesce(customer_name, '') ILIKE %s
               OR coalesce(service_address, '') ILIKE %s
               OR coalesce(district, '') ILIKE %s
            ORDER BY CASE WHEN supply_code = %s THEN 0 ELSE 1 END, supply_code
            LIMIT 500
            """,
            [search_pattern, search_pattern, search_pattern, search_pattern, normalized_search],
        )
        text_codes = [str(row["supply_code"]) for row in text_rows]
    matched_codes = list(dict.fromkeys([*text_codes, *(vector_supply_codes or [])]))[:600]
    rows = await fetch_all(
        pool,
        """
        WITH latest_period AS (
          SELECT max(period)::date AS period
          FROM (
            SELECT max(make_date(cd.period_year::int, cd.period_month::int, 1)) AS period
            FROM public.customer_debts cd WHERE lower(cd.concept) = 'consumo_agua'
            UNION ALL
            SELECT max(date_trunc('month', b.issue_date))::date AS period
            FROM public.customer_supply_billing_daily b
            WHERE b.issue_date IS NOT NULL AND lower(b.concept) = 'consumo_agua'
          ) available_periods
        ), monthly_candidates AS (
          SELECT cd.supply_code,
                 make_date(cd.period_year::int, cd.period_month::int, 1) AS period,
                 coalesce(cd.billed_volume_m3::float8, 0) AS volume,
                 0 AS source_priority, coalesce(cd.updated_at, cd.created_at) AS source_updated,
                 cd.id::text AS source_id
          FROM public.customer_debts cd CROSS JOIN latest_period latest
          WHERE lower(cd.concept) = 'consumo_agua'
            AND make_date(cd.period_year::int, cd.period_month::int, 1) >= latest.period - interval '4 months'
          UNION ALL
          SELECT b.supply_code, date_trunc('month', b.issue_date)::date,
                 coalesce(b.billed_volume_m3::float8, 0), 1,
                 coalesce(b.source_batch_date::timestamptz, b.imported_at), b.id::text
          FROM public.customer_supply_billing_daily b CROSS JOIN latest_period latest
          WHERE b.issue_date IS NOT NULL AND lower(b.concept) = 'consumo_agua'
            AND b.issue_date >= latest.period - interval '4 months'
        ), monthly AS (
          SELECT DISTINCT ON (supply_code, period) supply_code, period, volume
          FROM monthly_candidates
          ORDER BY supply_code, period, source_priority, source_updated DESC NULLS LAST, source_id DESC
        ), supply_context AS (
          SELECT cs.supply_code,
                 CASE
                   WHEN %s::text = 'supply' THEN 'supply:' || cs.supply_code
                   WHEN nullif(regexp_replace(coalesce(cs.id_doc_number, ''), '[^[:alnum:]]', '', 'g'), '') IS NOT NULL
                     THEN 'document:' || upper(regexp_replace(cs.id_doc_number, '[^[:alnum:]]', '', 'g'))
                   WHEN nullif(trim(cs.customer_code), '') IS NOT NULL THEN 'customer-code:' || trim(cs.customer_code)
                   WHEN cs.customer_id IS NOT NULL THEN 'customer:' || cs.customer_id::text
                   WHEN nullif(trim(cs.customer_name), '') IS NOT NULL
                     THEN 'name:' || lower(regexp_replace(cs.customer_name, '[^[:alnum:]]', '', 'g'))
                   ELSE 'supply:' || cs.supply_code
                 END AS customer_key,
                 CASE
                   WHEN %s::text = 'supply' THEN 'supply:' || cs.supply_code
                   WHEN nullif(link.cup_code, '') IS NOT NULL THEN 'cup:' || link.cup_code
                   WHEN nullif(cs.lot_code, '') IS NOT NULL
                     THEN 'lot:' || coalesce(cs.district, '') || ':' || cs.lot_code
                   ELSE 'supply:' || cs.supply_code
                 END AS property_key,
                 cs.customer_name, cs.service_address, cs.district, cs.is_primary,
                 CASE
                   WHEN lower(concat_ws(' ', coalesce(cs.segment, ''), coalesce(cs.office_name, '')))
                     LIKE ANY (ARRAY['%%grandes clientes%%', '%%grandes_clientes%%']) THEN 'grandes_clientes'
                   WHEN lower(concat_ws(' ', coalesce(cs.segment, ''), coalesce(cs.office_name, '')))
                     LIKE ANY (ARRAY['%%fuente propia%%', '%%fuente_propia%%']) THEN 'fuente_propia'
                   ELSE 'operativo'
                 END AS classification_key,
                 sl.geom
          FROM public.customer_supplies cs
          LEFT JOIN public.gis_supply_lot_links link ON link.supply_id = cs.id
          LEFT JOIN public.gis_supply_locations sl ON sl.supply_id = cs.id
        ), group_members AS (
          SELECT customer_key, property_key,
                 (array_agg(supply_code ORDER BY is_primary DESC NULLS LAST, supply_code))[1]
                   AS representative_supply_code,
                 array_agg(supply_code ORDER BY supply_code) AS supply_codes,
                 count(*)::int AS supply_count,
                 coalesce(jsonb_agg(
                   jsonb_build_object(
                     'supplyCode', supply_code,
                     'geometry', CASE WHEN geom IS NOT NULL THEN ST_AsGeoJSON(geom)::jsonb ELSE NULL END
                   ) ORDER BY supply_code
                 ), '[]'::jsonb) AS supply_points,
                 max(customer_name) AS customer_name,
                 max(service_address) AS service_address,
                 max(district) AS district,
                 CASE
                   WHEN bool_or(classification_key = 'grandes_clientes') THEN 'grandes_clientes'
                   WHEN bool_or(classification_key = 'fuente_propia') THEN 'fuente_propia'
                   ELSE 'operativo'
                 END AS classification_key,
                 CASE WHEN count(geom) > 0
                   THEN ST_AsGeoJSON(ST_Centroid(ST_Collect(geom)))::jsonb
                   ELSE NULL
                 END AS geometry
          FROM supply_context
          GROUP BY customer_key, property_key
        ), property_monthly AS (
          SELECT context.customer_key, context.property_key, monthly.period,
                 sum(monthly.volume)::float8 AS volume
          FROM monthly
          JOIN supply_context context ON context.supply_code = monthly.supply_code
          GROUP BY context.customer_key, context.property_key, monthly.period
        ), property_reference AS (
          SELECT current.customer_key, current.property_key, current.period, current.volume,
                 percentile_cont(0.5) WITHIN GROUP (ORDER BY nullif(previous.volume, 0)) AS prior_median
          FROM property_monthly current
          LEFT JOIN property_monthly previous
            ON previous.customer_key = current.customer_key
           AND previous.property_key = current.property_key
           AND previous.period >= current.period - interval '3 months'
           AND previous.period < current.period
          GROUP BY current.customer_key, current.property_key, current.period, current.volume
        ), compared AS (
          SELECT customer_key, property_key, period, volume, prior_median,
                 lag(period) OVER timeline AS previous_period,
                 lag(volume) OVER timeline AS previous_volume
          FROM property_reference
          WINDOW timeline AS (PARTITION BY customer_key, property_key ORDER BY period)
        ), matches AS (
          SELECT compared.*
          FROM compared
          WHERE previous_period = period - interval '1 month'
            AND previous_volume >= 5
            AND prior_median >= 5
            AND volume <= greatest(2, prior_median * 0.15)
            AND period = (SELECT latest.period FROM latest_period latest)
        ), enriched AS (
          SELECT members.representative_supply_code AS "supplyCode",
                 members.supply_codes AS "supplyCodes",
                 members.supply_count AS "supplyCount",
                 members.supply_points AS "supplyPoints",
                 members.property_key AS "propertyCode",
                 members.customer_name AS "customerName",
                 members.service_address AS "serviceAddress",
                 members.district,
                 matches.period::text AS period,
                 matches.volume AS "currentVolume",
                 matches.prior_median AS "referenceVolume",
                 matches.volume / nullif(members.supply_count, 0) AS "averageCurrentVolume",
                 matches.prior_median / nullif(members.supply_count, 0) AS "averageReferenceVolume",
                 round((1 - matches.volume / nullif(matches.prior_median, 0)) * 100)::int AS "dropPercent",
                 CASE WHEN matches.volume = 0 THEN 'zero' ELSE 'extremely_low' END AS kind,
                 members.classification_key,
                 CASE members.classification_key
                   WHEN 'grandes_clientes' THEN 'Grandes Clientes'
                   WHEN 'fuente_propia' THEN 'Fuente Propia'
                   ELSE 'Operativo'
                 END AS classification,
                 %s::text AS "analysisScope",
                 members.geometry
          FROM matches
          JOIN group_members members
            ON members.customer_key = matches.customer_key
           AND members.property_key = matches.property_key
        ), filtered AS (
          SELECT enriched.*
          FROM enriched
          WHERE (%s::text = 'supply' OR "supplyCount" > 1)
            AND (%s::text IS NULL OR classification_key = %s::text)
            AND (%s::text IS NULL OR kind = %s::text)
            AND (%s::text = '' OR lower(coalesce(district, '')) = lower(%s::text))
            AND (%s::boolean = false OR "supplyCodes" && %s::text[])
        )
        SELECT filtered.*, count(*) OVER()::int AS total
        FROM filtered
        ORDER BY period DESC, "dropPercent" DESC, "supplyCount" DESC, "supplyCode"
        LIMIT %s OFFSET %s
        """,
        [
            safe_analysis_scope, safe_analysis_scope, safe_analysis_scope,
            safe_analysis_scope,
            classification, classification,
            kind, kind,
            normalized_district, normalized_district,
            bool(normalized_search), matched_codes,
            safe_page_size, offset,
        ],
    )
    total = int(rows[0]["total"]) if rows else 0
    return {
        "total": total,
        "page": safe_page,
        "pageSize": safe_page_size,
        "items": [
            {key: value for key, value in row.items() if key not in {"total", "classification_key"}}
            for row in rows
        ],
    }
