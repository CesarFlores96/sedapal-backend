from psycopg_pool import AsyncConnectionPool

from app.repositories.inspections import fetch_inspections
from app.schemas.common import PaginatedDataResponse
from app.services.shared import build_paginated_response


async def get_inspections_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    supply_code: str | None,
    search: str | None = None,
) -> PaginatedDataResponse:
    data = await fetch_inspections(
        pool=pool,
        page=page,
        page_size=page_size,
        supply_code=supply_code,
        search=search,
    )
    return build_paginated_response(page=page, page_size=page_size, data=data)
