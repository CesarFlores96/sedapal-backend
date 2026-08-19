from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

ENSURE_OTA_UPDATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.ota_updates (
    -- Un publish genera UN id compartido por las filas ios/android de esa
    -- misma corrida (mismo bundle_dir en disco); por eso la PK es compuesta
    -- (id, platform) y no id solo.
    id              UUID NOT NULL,
    runtime_version VARCHAR(32) NOT NULL,
    platform        VARCHAR(10) NOT NULL CHECK (platform IN ('ios', 'android')),
    channel         VARCHAR(40) NOT NULL DEFAULT 'production',
    manifest_json   JSONB NOT NULL,
    bundle_dir      TEXT NOT NULL,
    commit_message  TEXT,
    commit_sha      VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at  TIMESTAMPTZ,
    status          VARCHAR(20) NOT NULL DEFAULT 'published'
                       CHECK (status IN ('published', 'superseded', 'rolled_back')),
    PRIMARY KEY (id, platform)
);
"""

ENSURE_OTA_UPDATES_UNIQUE_PUBLISHED_IDX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS ota_updates_one_published_idx
    ON public.ota_updates (channel, platform, runtime_version)
    WHERE status = 'published';
"""

ENSURE_OTA_UPDATES_LOOKUP_IDX_SQL = """
CREATE INDEX IF NOT EXISTS ota_updates_lookup_idx
    ON public.ota_updates (channel, platform, runtime_version, created_at DESC);
"""

_ota_updates_table_ready = False


async def ensure_ota_updates_table(pool: AsyncConnectionPool) -> None:
    global _ota_updates_table_ready
    if _ota_updates_table_ready:
        return

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(ENSURE_OTA_UPDATES_TABLE_SQL)
            await cursor.execute(ENSURE_OTA_UPDATES_UNIQUE_PUBLISHED_IDX_SQL)
            await cursor.execute(ENSURE_OTA_UPDATES_LOOKUP_IDX_SQL)
        await connection.commit()

    _ota_updates_table_ready = True


async def get_published_update(
    pool: AsyncConnectionPool, *, channel: str, platform: str, runtime_version: str
) -> dict | None:
    await ensure_ota_updates_table(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT id, runtime_version, platform, channel, manifest_json,
                       created_at, rolled_back_at, status
                FROM public.ota_updates
                WHERE channel = %s AND platform = %s AND runtime_version = %s
                  AND status = 'published'
                LIMIT 1;
                """,
                [channel, platform, runtime_version],
            )
            return await cursor.fetchone()


async def get_latest_rolled_back(
    pool: AsyncConnectionPool, *, channel: str, platform: str, runtime_version: str
) -> dict | None:
    """Fila 'rolled_back' mas reciente para ese grupo, solo relevante si no hay
    ningun 'published' vigente (si lo hay, ese manda)."""

    await ensure_ota_updates_table(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT id, rolled_back_at
                FROM public.ota_updates
                WHERE channel = %s AND platform = %s AND runtime_version = %s
                  AND status = 'rolled_back'
                ORDER BY rolled_back_at DESC
                LIMIT 1;
                """,
                [channel, platform, runtime_version],
            )
            return await cursor.fetchone()


async def insert_published_update(
    pool: AsyncConnectionPool,
    *,
    update_id: str,
    runtime_version: str,
    platform: str,
    channel: str,
    manifest_json: dict,
    bundle_dir: str,
    commit_message: str | None,
    commit_sha: str | None,
) -> None:
    """Marca 'superseded' cualquier publicado previo del mismo grupo e inserta
    el nuevo como 'published', en una sola transaccion (evita violar el indice
    unico parcial bajo publicaciones concurrentes)."""

    await ensure_ota_updates_table(pool)

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.ota_updates
                SET status = 'superseded'
                WHERE channel = %s AND platform = %s AND runtime_version = %s
                  AND status = 'published';
                """,
                [channel, platform, runtime_version],
            )
            await cursor.execute(
                """
                INSERT INTO public.ota_updates
                    (id, runtime_version, platform, channel, manifest_json,
                     bundle_dir, commit_message, commit_sha, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'published');
                """,
                [
                    update_id,
                    runtime_version,
                    platform,
                    channel,
                    Jsonb(manifest_json),
                    bundle_dir,
                    commit_message,
                    commit_sha,
                ],
            )
        await connection.commit()


async def mark_rolled_back(
    pool: AsyncConnectionPool, *, channel: str, platform: str, runtime_version: str
) -> bool:
    await ensure_ota_updates_table(pool)

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.ota_updates
                SET status = 'rolled_back', rolled_back_at = now()
                WHERE channel = %s AND platform = %s AND runtime_version = %s
                  AND status = 'published';
                """,
                [channel, platform, runtime_version],
            )
            updated = cursor.rowcount > 0
        await connection.commit()
    return updated


async def activate_update(
    pool: AsyncConnectionPool,
    *,
    update_id: str,
    channel: str,
    platform: str,
    runtime_version: str,
) -> bool:
    """Reactiva una fila existente (cualquier status) como 'published',
    supersediendo el 'published' vigente del mismo grupo, si lo hay."""

    await ensure_ota_updates_table(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT id FROM public.ota_updates
                WHERE id = %s AND channel = %s AND platform = %s AND runtime_version = %s;
                """,
                [update_id, channel, platform, runtime_version],
            )
            target = await cursor.fetchone()
            if target is None:
                return False

            await cursor.execute(
                """
                UPDATE public.ota_updates
                SET status = 'superseded'
                WHERE channel = %s AND platform = %s AND runtime_version = %s
                  AND status = 'published';
                """,
                [channel, platform, runtime_version],
            )
            await cursor.execute(
                """
                UPDATE public.ota_updates
                SET status = 'published', rolled_back_at = NULL
                WHERE id = %s;
                """,
                [update_id],
            )
        await connection.commit()
    return True
