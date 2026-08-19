from psycopg_pool import AsyncConnectionPool

from app.repositories.data_sources import (
    fetch_contrastations,
    fetch_data_sources_overview,
    fetch_meter_installations,
    fetch_service_orders,
)
from app.schemas.common import PaginatedDataResponse
from app.services.shared import build_paginated_response


async def get_data_sources_overview(pool: AsyncConnectionPool) -> dict[str, object]:
    return {
        "success": True,
        "datasets": await fetch_data_sources_overview(pool),
    }


async def get_contrastations_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_contrastations(pool=pool, page=page, page_size=page_size, search=search)
    return build_paginated_response(page=page, page_size=page_size, data=data)


async def get_meter_installations_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_meter_installations(pool=pool, page=page, page_size=page_size, search=search)
    return build_paginated_response(page=page, page_size=page_size, data=data)


async def get_service_orders_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_service_orders(pool=pool, page=page, page_size=page_size, search=search)
    return build_paginated_response(page=page, page_size=page_size, data=data)
