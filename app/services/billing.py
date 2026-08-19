import asyncio

from psycopg_pool import AsyncConnectionPool

from app.repositories.billing import fetch_billing_customers
from app.repositories.facturacion import fetch_facturacion
from app.repositories.inspections import fetch_inspections
from app.repositories.readings import fetch_readings
from app.repositories.reports import (
    fetch_report_supply_anomalies,
    fetch_report_supply_meters,
)
from app.schemas.common import PaginatedDataResponse
from app.schemas.consumption_analysis import ConsumptionAnalysisResponse
from app.services.consumption_analysis import (
    MIN_OBSERVATIONS_DEFAULT,
    analyze_supply_consumption,
    build_operational_context,
)
from app.services.shared import build_paginated_response


async def get_billing_customers_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
) -> PaginatedDataResponse:
    data = await fetch_billing_customers(pool=pool, page=page, page_size=page_size)
    return build_paginated_response(page=page, page_size=page_size, data=data)


async def get_billing_detail_page(
    pool: AsyncConnectionPool,
    supply_code: str,
    page: int,
    page_size: int,
) -> PaginatedDataResponse:
    data = await fetch_facturacion(pool=pool, suministro=supply_code, page=page, page_size=page_size)
    return build_paginated_response(page=page, page_size=page_size, data=data)


def _build_monthly_water(rows: list[dict]) -> dict[int, dict[int, float | None]]:
    """Aggregate raw facturación rows into ``year -> month -> water volume``.

    Mirrors the frontend ``getBillingDetailAction`` transform exactly so the
    numbers match what the chart already shows:

    * water = the exact ``consumo_agua`` concept;
    * duplicate period/concept rows are ignored defensively (the repository
      already returns the canonical, latest source row);
    * a present month keeps its real value (including ``0.0``); an absent month
      stays out of the map entirely (``None`` when read back) -- ``NULL`` is
      never coerced to ``0``.
    """
    seen: set[tuple] = set()
    monthly: dict[int, dict[int, float | None]] = {}

    for row in rows:
        year = _safe_int(row.get("period_year"))
        month = _safe_int(row.get("period_month"))
        if year is None or month is None:
            continue
        concept = (row.get("concept") or "").strip()
        volume = round(float(row.get("billed_volume_m3") or 0), 2)
        normalized_concept = concept.lower()
        if normalized_concept != "consumo_agua":
            continue
        dedup_key = (year, month, normalized_concept)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        bucket = monthly.setdefault(year, {})
        bucket[month] = volume

    return monthly


async def _safe_gather(coro):
    """Await a repository coroutine, returning ``None`` on failure so a missing
    or broken operational source degrades to ``not_available`` instead of
    breaking the whole analysis."""
    try:
        return await coro
    except Exception:  # noqa: BLE001 - operational context is best-effort
        return None


async def get_consumption_analysis(
    pool: AsyncConnectionPool,
    supply_code: str,
    min_obs: int = MIN_OBSERVATIONS_DEFAULT,
) -> ConsumptionAnalysisResponse:
    billing_rows = await fetch_facturacion(pool=pool, suministro=supply_code)
    monthly_water = _build_monthly_water(billing_rows)

    readings, meters, anomalies, inspections = await asyncio.gather(
        _safe_gather(fetch_readings(pool, page=1, page_size=500, supply_code=supply_code)),
        _safe_gather(fetch_report_supply_meters(pool, supply_code=supply_code)),
        _safe_gather(fetch_report_supply_anomalies(pool, supply_code=supply_code)),
        _safe_gather(fetch_inspections(pool, page=1, page_size=200, supply_code=supply_code)),
    )

    work_orders = None
    if inspections is not None:
        work_orders = [
            {
                "code": row.get("work_order_number"),
                "status": row.get("service_status"),
                "performed_at": row.get("visit_date"),
            }
            for row in inspections
            if row.get("work_order_number")
        ]

    context = build_operational_context(
        readings=readings,
        meters=meters,
        work_orders=work_orders,
        inspections=inspections,
        registered_anomalies=anomalies,
    )

    result = analyze_supply_consumption(
        supply_code=supply_code,
        monthly_water=monthly_water,
        operational=context,
        min_obs=min_obs,
    )
    return ConsumptionAnalysisResponse.model_validate(result)


def _safe_int(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
