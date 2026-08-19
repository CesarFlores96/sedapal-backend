from psycopg_pool import AsyncConnectionPool

from app.repositories.reports import fetch_report_master_page


async def get_report_master_page(
    *,
    pool: AsyncConnectionPool,
    page: int,
    page_size: int,
    search: str,
    consumption_filter: dict | None,
) -> dict:
    return await fetch_report_master_page(
        pool=pool,
        page=page,
        page_size=page_size,
        search=search,
        consumption_filter=consumption_filter,
    )
