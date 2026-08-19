import asyncio

import numpy as np
from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import execute_fetch_all_dict, fetch_all_dict


_embedding_index: tuple[list[str], np.ndarray] | None = None
_embedding_index_lock = asyncio.Lock()


def normalize_location_source(value: str | None) -> str:
    if not value:
        return "catastro"
    lower = value.strip().lower()
    if "geo" in lower or "gps" in lower:
        return lower
    return "catastro"


async def update_supply_location(
    pool: AsyncConnectionPool,
    supply_id: str,
    address: str | None,
    latitude: float,
    longitude: float,
    source: str,
) -> dict | None:
    rows = await execute_fetch_all_dict(
        pool,
        """
        UPDATE public.customer_supplies
        SET
            geolocation_address = %s,
            latitude = %s,
            longitude = %s,
            location_source = %s,
            location_quality = 'ubicado',
            updated_at = NOW()
        WHERE id = %s
        RETURNING
            id,
            supply_code,
            geolocation_address,
            latitude,
            longitude,
            location_source;
        """,
        [address, latitude, longitude, normalize_location_source(source), supply_id],
    )
    return rows[0] if rows else None


async def update_supply_location_by_code(
    pool: AsyncConnectionPool,
    supply_code: str,
    latitude: float,
    longitude: float,
    source: str,
) -> dict | None:
    rows = await execute_fetch_all_dict(
        pool,
        """
        UPDATE public.customer_supplies
        SET
            latitude = %s,
            longitude = %s,
            location_source = %s,
            location_quality = 'ubicado',
            updated_at = NOW()
        WHERE supply_code = %s
        RETURNING
            id,
            supply_code,
            geolocation_address,
            latitude,
            longitude,
            location_source;
        """,
        [latitude, longitude, normalize_location_source(source), supply_code.strip()],
    )
    return rows[0] if rows else None


async def search_supplies_by_code(
    pool: AsyncConnectionPool,
    code: str,
    limit: int = 50,
) -> list[dict]:
    normalized_code = code.strip()
    if not normalized_code:
        return []

    return await fetch_all_dict(
        pool,
        """
        SELECT
            supply_code,
            customer_name,
            service_address,
            district
        FROM public.customer_supplies
        WHERE supply_code ILIKE %s
        ORDER BY supply_code ASC
        LIMIT %s;
        """,
        [f"{normalized_code}%", limit],
    )


async def get_supply_context(pool: AsyncConnectionPool, code: str) -> dict | None:
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
          cs.id, cs.supply_code, cs.customer_id, cs.customer_name,
          cs.service_address, cs.district, cs.latitude, cs.longitude,
          cs.meter_code, cs.segment, cs.supply_status, cs.geolocation_address,
          cs.location_source, cs.office_name, cs.id_doc_number, cs.reference,
          cs.cua, cs.meter_diameter, cs.connection_diameter,
          c.business_name, c.first_name, c.last_name, c.full_name,
          c.payer_classification
        FROM public.customer_supplies cs
        LEFT JOIN public.customers c ON c.id = cs.customer_id
        WHERE cs.supply_code = %s
        LIMIT 1
        """,
        [code.strip()],
    )
    return rows[0] if rows else None


async def get_supply_coordinates(pool: AsyncConnectionPool, codes: list[str]) -> list[dict]:
    normalized = list(dict.fromkeys(code.strip() for code in codes if code.strip()))[:2000]
    if not normalized:
        return []
    return await fetch_all_dict(
        pool,
        """
        SELECT supply_code, latitude, longitude
        FROM public.customer_supplies
        WHERE supply_code = ANY(%s)
          AND latitude IS NOT NULL AND longitude IS NOT NULL
        """,
        [normalized],
    )


async def search_supplies_vectorial(
    local_pool: AsyncConnectionPool,
    query_embedding: list[float],
    limit: int = 50,
) -> list[dict]:
    supply_codes, matrix = await _get_local_embedding_index(local_pool)
    if not supply_codes or matrix.shape[1] != len(query_embedding):
        return []

    query = np.asarray(query_embedding, dtype=np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0:
        return []
    scores = matrix @ (query / query_norm)
    selected = np.argsort(-scores)[:limit]
    matched_codes = [supply_codes[int(index)] for index in selected]

    rows = await fetch_all_dict(
        local_pool,
        """
        SELECT supply_code, customer_name, service_address, district
        FROM public.customer_supplies
        WHERE supply_code = ANY(%s)
        ORDER BY array_position(%s::text[], supply_code::text);
        """,
        [matched_codes, matched_codes],
    )
    return rows


async def _get_local_embedding_index(
    local_pool: AsyncConnectionPool,
) -> tuple[list[str], np.ndarray]:
    global _embedding_index
    if _embedding_index is not None:
        return _embedding_index

    async with _embedding_index_lock:
        if _embedding_index is not None:
            return _embedding_index

        rows = await fetch_all_dict(
            local_pool,
            """
            SELECT supply_code, embedding
            FROM public.supply_embeddings
            ORDER BY supply_code;
            """,
        )
        if not rows:
            _embedding_index = ([], np.empty((0, 0), dtype=np.float32))
            return _embedding_index

        codes = [str(row["supply_code"]) for row in rows]
        matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        _embedding_index = (codes, matrix / norms)
        return _embedding_index
