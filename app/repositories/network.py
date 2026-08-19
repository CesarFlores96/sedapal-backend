from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import execute_fetch_all_dict, fetch_all_dict


async def fetch_network_pipes(pool: AsyncConnectionPool, network_type: str | None) -> list[dict]:
    return await fetch_all_dict(
        pool,
        """
        SELECT
            id,
            network_type,
            network_level,
            material,
            diameter_mm,
            condition,
            COALESCE(length_m, ST_Length(geom::geography)) AS computed_length_m,
            notes
        FROM public.network_pipes
        WHERE (%s::text IS NULL OR network_type::text = %s::text)
        ORDER BY created_at DESC;
        """,
        [network_type, network_type],
    )


async def create_network_pipe(
    pool: AsyncConnectionPool,
    *,
    condition: str,
    diameter_mm: int | None,
    end_lat: float,
    end_lng: float,
    material: str,
    network_level: str,
    network_type: str,
    notes: str | None,
    start_lat: float,
    start_lng: float,
) -> dict:
    rows = await execute_fetch_all_dict(
        pool,
        """
        INSERT INTO public.network_pipes (
            network_type,
            network_level,
            material,
            diameter_mm,
            condition,
            length_m,
            geom,
            notes
        ) VALUES (
            %s::network_type,
            %s::network_level,
            %s::pipe_material,
            %s,
            %s::asset_condition,
            ST_Length(
                ST_SetSRID(
                    ST_MakeLine(
                        ST_MakePoint(%s, %s),
                        ST_MakePoint(%s, %s)
                    ),
                    4326
                )::geography
            ),
            ST_SetSRID(
                ST_MakeLine(
                    ST_MakePoint(%s, %s),
                    ST_MakePoint(%s, %s)
                ),
                4326
            ),
            %s
        )
        RETURNING id;
        """,
        [
            network_type,
            network_level,
            material,
            diameter_mm,
            condition,
            start_lng,
            start_lat,
            end_lng,
            end_lat,
            start_lng,
            start_lat,
            end_lng,
            end_lat,
            notes,
        ],
    )
    return {"id": rows[0]["id"]}
