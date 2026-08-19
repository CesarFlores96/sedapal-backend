from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def fetch_all(
    pool: AsyncConnectionPool, query: str, params: list[object] | tuple[object, ...] | None = None
) -> list[dict]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            if params is None:
                await cursor.execute(pgsql.SQL(query))
            else:
                await cursor.execute(query, params)
            return list(await cursor.fetchall())


async def fetch_one(
    pool: AsyncConnectionPool, query: str, params: list[object] | tuple[object, ...] | None = None
) -> dict | None:
    rows = await fetch_all(pool, query, params)
    return rows[0] if rows else None

