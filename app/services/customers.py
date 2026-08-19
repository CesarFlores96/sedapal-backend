from psycopg_pool import AsyncConnectionPool

from app.repositories.customers import fetch_customers
from app.schemas.common import PaginatedDataResponse


async def get_customers_page(
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str | None,
) -> PaginatedDataResponse:
    data, total = await fetch_customers(pool=pool, page=page, page_size=page_size, search=search)
    return PaginatedDataResponse(
        page=page,
        page_size=page_size,
        total_returned=len(data),
        total=total,
        data=data,
    )
