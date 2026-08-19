from psycopg_pool import AsyncConnectionPool

from app.repositories.meters import (
    fetch_meter_by_supply_code,
    fetch_meter_planning_stats,
    fetch_meter_registry,
    fetch_meter_registry_stats,
    fetch_meter_vencimiento_rows,
    fetch_meters,
    update_meter,
)
from app.schemas.common import PaginatedDataResponse
from app.services.shared import build_paginated_response


async def get_meters_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
    status: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_meters(pool=pool, page=page, page_size=page_size, search=search, status=status)
    return build_paginated_response(page=page, page_size=page_size, data=data)


async def get_meter_by_supply_code(pool: AsyncConnectionPool, supply_code: str) -> list[dict]:
    return await fetch_meter_by_supply_code(pool, supply_code)


async def get_meter_vencimiento_rows(pool: AsyncConnectionPool, status: str) -> list[dict]:
    return await fetch_meter_vencimiento_rows(pool, status)


async def get_meter_planning_stats(pool: AsyncConnectionPool) -> dict:
    return await fetch_meter_planning_stats(pool)


async def update_meter_fields(
    pool: AsyncConnectionPool,
    supply_code: str,
    meter_serial: str | None,
    diameter_mm: int | None,
    installation_date: str | None,
    supply_status: str | None,
    segment_name: str | None,
    meter_status: str | None,
) -> None:
    await update_meter(
        pool,
        supply_code=supply_code,
        meter_serial=meter_serial,
        diameter_mm=diameter_mm,
        installation_date=installation_date,
        supply_status=supply_status,
        segment_name=segment_name,
        meter_status=meter_status,
    )


async def get_meter_registry_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    registry_status: str | None = None,
    search: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_meter_registry(
        pool=pool, page=page, page_size=page_size, registry_status=registry_status, search=search
    )
    return build_paginated_response(page=page, page_size=page_size, data=data)


async def get_meter_registry_stats(pool: AsyncConnectionPool) -> list[dict]:
    return await fetch_meter_registry_stats(pool)
