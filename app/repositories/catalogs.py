from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import fetch_all_dict


async def fetch_supervision_catalogs(pool: AsyncConnectionPool) -> dict[int, dict[str, str]]:
    rows = await fetch_all_dict(
        pool,
        """
        SELECT catalog_number, code, description
        FROM public.supervision_code_catalog
        ORDER BY catalog_number, display_order, code;
        """,
    )

    catalog_map: dict[int, dict[str, str]] = {}

    for row in rows:
        catalog_number = int(row["catalog_number"])
        code = (row.get("code") or "").strip()
        description = (row.get("description") or "").strip()

        if not code:
            continue

        if catalog_number not in catalog_map:
            catalog_map[catalog_number] = {}

        catalog_map[catalog_number][code] = description

    return catalog_map
