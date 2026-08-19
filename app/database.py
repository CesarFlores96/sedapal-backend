import asyncio
import sys

from psycopg_pool import AsyncConnectionPool
from psycopg.errors import InsufficientPrivilege

from app.config import get_settings
from app.windows_credentials import read_generic_credential


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        import uvicorn.loops.asyncio as uvicorn_asyncio
        uvicorn_asyncio.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop
    except ImportError:
        pass


pool: AsyncConnectionPool | None = None
gis_pool: AsyncConnectionPool | None = None
martin_pool: AsyncConnectionPool | None = None
cadastral_pool: AsyncConnectionPool | None = None
supabase_pool: AsyncConnectionPool | None = None
SUPABASE_DATA_TABLES = frozenset({"supervision", "planillas"})


def create_pool() -> AsyncConnectionPool:
    settings = get_settings()
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={
            "autocommit": False,
            "prepare_threshold": None,
        },
    )


def _create_local_pool(conninfo: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"autocommit": False, "prepare_threshold": None},
    )


async def _gis_pool_has_required_access(candidate_pool: AsyncConnectionPool) -> bool:
    try:
        async with candidate_pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT
                      has_table_privilege(current_user, 'public.customer_supplies', 'SELECT'),
                      has_table_privilege(current_user, 'public.gis_supply_locations', 'INSERT,UPDATE,DELETE')
                    """
                )
                row = await cursor.fetchone()
    except InsufficientPrivilege:
        return False
    if not row:
        return False
    can_read_customer_supplies, can_write_gis_supply_locations = row
    return bool(can_read_customer_supplies and can_write_gis_supply_locations)


async def open_specialized_pools() -> None:
    global gis_pool, martin_pool, cadastral_pool
    settings = get_settings()
    cadastral_url = read_generic_credential(
        "business-database-url.pe.sedapal.gis"
    ) or settings.gis_database_url or settings.database_url
    gis_url = settings.gis_database_url or read_generic_credential(
        "business-database-url.pe.sedapal.gis"
    ) or settings.database_url
    martin_url = settings.martin_database_url or read_generic_credential(
        "martin-database-url.pe.sedapal.gis"
    ) or settings.database_url
    if gis_pool is None:
        gis_pool = _create_local_pool(gis_url)
    if cadastral_pool is None:
        # La conexion comercial puede leer el catastro normalizado, pero no
        # escribe ubicaciones. Se conserva separada del pool GIS operativo para
        # generar teselas corregidas sin elevar permisos en PostgreSQL.
        cadastral_pool = _create_local_pool(cadastral_url)
    if martin_pool is None:
        martin_pool = _create_local_pool(martin_url)
    await cadastral_pool.open()
    await gis_pool.open()
    if gis_url != settings.database_url and not await _gis_pool_has_required_access(gis_pool):
        await gis_pool.close()
        gis_pool = _create_local_pool(settings.database_url)
        await gis_pool.open()
        print(
            "[WARN] GIS_DATABASE_URL sin acceso suficiente a customer_supplies/gis_supply_locations; "
            "se usa DATABASE_URL para el pool GIS."
        )
    await martin_pool.open()


async def close_specialized_pools() -> None:
    global gis_pool, martin_pool, cadastral_pool
    if gis_pool is not None:
        await gis_pool.close()
        gis_pool = None
    if martin_pool is not None:
        await martin_pool.close()
        martin_pool = None
    if cadastral_pool is not None:
        await cadastral_pool.close()
        cadastral_pool = None


def get_gis_pool() -> AsyncConnectionPool:
    if gis_pool is None:
        raise RuntimeError("GIS database pool is not initialized.")
    return gis_pool


def get_martin_pool() -> AsyncConnectionPool:
    if martin_pool is None:
        raise RuntimeError("Martin database pool is not initialized.")
    return martin_pool


def get_cadastral_pool() -> AsyncConnectionPool:
    if cadastral_pool is None:
        raise RuntimeError("Cadastral database pool is not initialized.")
    return cadastral_pool


async def open_pool() -> None:
    global pool
    if pool is None:
        pool = create_pool()
    await pool.open()


async def close_pool() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


def get_pool() -> AsyncConnectionPool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized.")
    return pool


def create_supabase_pool() -> AsyncConnectionPool | None:
    settings = get_settings()
    conninfo = read_generic_credential("supabase-boundary-database-url.pe.sedapal") or settings.supabase_database_url
    if not conninfo:
        return None
    return AsyncConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={
            "autocommit": False,
            "prepare_threshold": None,
        },
    )


async def open_supabase_pool() -> None:
    global supabase_pool
    if supabase_pool is None:
        supabase_pool = create_supabase_pool()
    if supabase_pool is not None:
        await supabase_pool.open()


async def close_supabase_pool() -> None:
    global supabase_pool
    if supabase_pool is not None:
        await supabase_pool.close()
        supabase_pool = None


def get_supabase_pool(table: str) -> AsyncConnectionPool:
    if table not in SUPABASE_DATA_TABLES:
        raise RuntimeError(f"La tabla remota {table!r} no pertenece a la allowlist de Supabase.")
    if supabase_pool is None:
        raise RuntimeError("Falta SUPABASE_DATABASE_URL: el pool de Supabase no esta inicializado.")
    return supabase_pool
