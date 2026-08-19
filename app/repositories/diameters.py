from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


DIAMETER_VALUES = ["10", "15", "20", "25", "80", "100", "50", "200", "40"]

ENSURE_DIAMETER_CATALOG_SQL = """
CREATE TABLE IF NOT EXISTS public.diameter_catalog (
  value_text text primary key,
  diameter_mm integer not null unique,
  display_order integer not null unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  CONSTRAINT diameter_catalog_positive_mm CHECK (diameter_mm > 0)
);
"""

_diameter_catalog_ready = False


async def ensure_diameter_catalog(pool: AsyncConnectionPool) -> None:
    global _diameter_catalog_ready
    if _diameter_catalog_ready:
        return

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(ENSURE_DIAMETER_CATALOG_SQL)
            await cursor.execute("SELECT count(*) AS total FROM public.diameter_catalog;")
            row = await cursor.fetchone()
            existing_count = row[0] if row else 0

            # Si ya existe un catalogo curado (p.ej. cargado a mano en Supabase), no lo
            # pisamos con el seed por defecto: ademas de duplicar valores con otro
            # formato de value_text ("15" vs "15 mm"), el UPSERT de abajo solo cubre
            # conflicto en value_text y puede fallar por la restriccion UNIQUE de
            # diameter_mm cuando el mm ya existe bajo un value_text distinto.
            if existing_count == 0:
                for index, value in enumerate(DIAMETER_VALUES, start=1):
                    await cursor.execute(
                        """
                        INSERT INTO public.diameter_catalog (value_text, diameter_mm, display_order)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING;
                        """,
                        [value, int(value), index],
                    )
        await connection.commit()

    _diameter_catalog_ready = True


async def list_diameter_catalog(pool: AsyncConnectionPool) -> list[dict]:
    await ensure_diameter_catalog(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT value_text, diameter_mm, display_order, vida_util_anios
                FROM public.diameter_catalog
                ORDER BY display_order, diameter_mm;
                """
            )
            rows = await cursor.fetchall()

    return [
        {
            "diameterMm": row["diameter_mm"],
            "displayOrder": row["display_order"],
            "value": row["value_text"],
            "vidaUtilAnios": row["vida_util_anios"],
        }
        for row in rows
    ]


async def update_diameter_vida_util(
    pool: AsyncConnectionPool, diameter_mm: int, vida_util_anios: int | None
) -> bool:
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.diameter_catalog
                SET vida_util_anios = %s, updated_at = now()
                WHERE diameter_mm = %s
                """,
                [vida_util_anios, diameter_mm],
            )
            updated = cursor.rowcount > 0
        await connection.commit()
    return updated


async def diameter_exists(pool: AsyncConnectionPool, diameter_mm: int) -> bool:
    await ensure_diameter_catalog(pool)

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT 1
                FROM public.diameter_catalog
                WHERE diameter_mm = %s
                LIMIT 1;
                """,
                [diameter_mm],
            )
            return await cursor.fetchone() is not None
