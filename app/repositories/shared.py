from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def fetch_all_dict(
    pool: AsyncConnectionPool,
    query: str,
    params: tuple | list | None = None,
) -> list[dict]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            if params:
                await cursor.execute(query, params)
            else:
                # Use pgsql.SQL to avoid psycopg parsing '%' literals
                # (e.g. LIKE '%SEDAPAL%') as parameter placeholders.
                await cursor.execute(pgsql.SQL(query))
            return await cursor.fetchall()


async def fetch_value(
    pool: AsyncConnectionPool,
    query: str,
    params: tuple | list | None = None,
):
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            if params:
                await cursor.execute(query, params)
            else:
                await cursor.execute(pgsql.SQL(query))
            row = await cursor.fetchone()
    return row[0] if row else None


async def execute_fetch_all_dict(
    pool: AsyncConnectionPool,
    query: str,
    params: tuple | list | None = None,
) -> list[dict]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            if params:
                await cursor.execute(query, params)
            else:
                await cursor.execute(pgsql.SQL(query))
            rows = await cursor.fetchall()
        await connection.commit()
    return rows


async def execute_statement(
    pool: AsyncConnectionPool,
    query: str,
    params: tuple | list | None = None,
) -> None:
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            if params:
                await cursor.execute(query, params)
            else:
                await cursor.execute(pgsql.SQL(query))
        await connection.commit()
