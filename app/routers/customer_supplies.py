from fastapi import APIRouter, Body, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.database import get_pool
from app.repositories.customer_supplies import (
    get_supply_context,
    get_supply_coordinates,
    search_supplies_by_code,
    update_supply_location,
    update_supply_location_by_code,
    search_supplies_vectorial,
)
from app.services.embeddings import get_embedding


class SupplyLocationUpdateRequest(BaseModel):
    address: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: str = Field(default="geolocalizacion", min_length=1, max_length=80)


class SupplyCodesRequest(BaseModel):
    supplyCodes: list[str] = Field(default_factory=list, max_length=2000)


class SupplyCoordinatesUpdateRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: str = Field(default="gps-planilla", min_length=1, max_length=80)


router = APIRouter(prefix="/customer-supplies", tags=["customer-supplies"])


@router.get("/search")
async def get_supply_search_results(
    code: str = Query(default="", min_length=0, max_length=40),
    limit: int = Query(default=50, ge=1, le=50),
) -> list[dict]:
    if not code.strip():
        return []

    return await search_supplies_by_code(get_pool(), code=code, limit=limit)


@router.get("/search-vector")
async def get_supply_search_vector_results(
    query: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=50, ge=1, le=50),
) -> list[dict]:
    if not query.strip():
        return []
    query_embedding = get_embedding(query)
    return await search_supplies_vectorial(get_pool(), query_embedding=query_embedding, limit=limit)


@router.patch("/{supply_id}/location")
async def patch_supply_location(
    supply_id: str = Path(..., min_length=1, max_length=80),
    payload: SupplyLocationUpdateRequest = Body(...),
) -> dict:
    result = await update_supply_location(
        get_pool(),
        supply_id=supply_id.strip(),
        address=payload.address.strip() if payload.address else None,
        latitude=payload.latitude,
        longitude=payload.longitude,
        source=payload.source,
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suministro no encontrado.")
    return result


@router.patch("/by-code/{supply_code}/location")
async def patch_supply_location_by_code(
    supply_code: str = Path(..., min_length=1, max_length=80),
    payload: SupplyCoordinatesUpdateRequest = Body(...),
) -> dict:
    result = await update_supply_location_by_code(
        get_pool(),
        supply_code=supply_code.strip(),
        latitude=payload.latitude,
        longitude=payload.longitude,
        source=payload.source,
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suministro no encontrado.")
    return result


@router.get("/detail/{supply_code}")
async def get_supply(supply_code: str = Path(..., min_length=1, max_length=80)) -> dict:
    result = await get_supply_context(get_pool(), supply_code)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suministro no encontrado.")
    return result


@router.post("/coordinates/batch")
async def post_supply_coordinates(payload: SupplyCodesRequest = Body(...)) -> dict:
    return {"data": await get_supply_coordinates(get_pool(), payload.supplyCodes)}


@router.get("/by-code/{supply_code}/cadastre")
async def get_supply_cadastre(supply_code: str = Path(..., min_length=1, max_length=80)) -> dict:
    """Devuelve área (m²) y perímetro (m) del lote/manzana vinculado al NIS, calculados por PostGIS.

    Flujo de resolución (primer resultado no nulo):
    1. Lote catastral enlazado por cup_code en gis_supply_lot_links.
    2. Lote catastral que contiene espacialmente la ubicación del suministro.
    3. Manzana catastral que contiene espacialmente la ubicación del suministro.
    """
    from app.database import get_gis_pool
    from app.repositories.shared import fetch_all_dict

    code = supply_code.strip()
    rows = await fetch_all_dict(
        get_gis_pool(),
        """
        WITH supply AS (
            SELECT cs.id, cs.supply_code,
                   sl.geom AS supply_geom
            FROM public.customer_supplies cs
            LEFT JOIN public.gis_supply_locations sl ON sl.supply_id = cs.id
            WHERE cs.supply_code = %s
            LIMIT 1
        ),
        lot_via_link AS (
            SELECT
                l.id,
                b.block_code,
                l.lot_code,
                ST_Area(l.geom::geography)::float8  AS area_m2,
                ST_Perimeter(l.geom::geography)::float8 AS perimeter_m
            FROM supply s
            JOIN public.gis_supply_lot_links link ON link.supply_id = s.id
            JOIN public.gis_cadastral_lot_units unit ON unit.cup_code = link.cup_code
            JOIN public.gis_cadastral_lot_geometries bridge ON bridge.cup_code = unit.cup_code
            JOIN public.gis_lots l ON l.id = bridge.gis_lot_id
            LEFT JOIN public.gis_blocks b ON b.id = l.block_id
            LIMIT 1
        ),
        lot_via_spatial AS (
            SELECT
                l.id,
                b.block_code,
                l.lot_code,
                ST_Area(l.geom::geography)::float8  AS area_m2,
                ST_Perimeter(l.geom::geography)::float8 AS perimeter_m
            FROM supply s
            JOIN public.gis_lots l ON s.supply_geom IS NOT NULL
                AND ST_Covers(
                    ST_Translate(
                        l.geom,
                        COALESCE((SELECT delta_lng FROM public.gis_geometry_corrections gc
                                  JOIN public.gis_blocks b2 ON b2.id = l.block_id
                                  WHERE gc.target_kind = 'block' AND gc.block_id = b2.id LIMIT 1), 0)
                        + COALESCE((SELECT delta_lng FROM public.gis_geometry_corrections gc
                                   WHERE gc.target_kind = 'lot' AND gc.lot_id = l.id LIMIT 1), 0),
                        COALESCE((SELECT delta_lat FROM public.gis_geometry_corrections gc
                                  JOIN public.gis_blocks b2 ON b2.id = l.block_id
                                  WHERE gc.target_kind = 'block' AND gc.block_id = b2.id LIMIT 1), 0)
                        + COALESCE((SELECT delta_lat FROM public.gis_geometry_corrections gc
                                   WHERE gc.target_kind = 'lot' AND gc.lot_id = l.id LIMIT 1), 0)
                    ),
                    s.supply_geom
                )
            LEFT JOIN public.gis_blocks b ON b.id = l.block_id
            ORDER BY ST_Area(l.geom::geography)
            LIMIT 1
        ),
        block_via_spatial AS (
            SELECT
                b.id,
                b.block_code,
                NULL::text AS lot_code,
                ST_Area(b.geom::geography)::float8  AS area_m2,
                ST_Perimeter(b.geom::geography)::float8 AS perimeter_m
            FROM supply s
            JOIN public.gis_blocks b ON s.supply_geom IS NOT NULL
                AND ST_Covers(b.geom, s.supply_geom)
            ORDER BY ST_Area(b.geom::geography)
            LIMIT 1
        )
        SELECT
            COALESCE(ll.block_code, ls.block_code, bs.block_code) AS block_code,
            COALESCE(ll.lot_code,   ls.lot_code)                   AS lot_code,
            COALESCE(ll.area_m2,    ls.area_m2,    bs.area_m2)    AS area_m2,
            COALESCE(ll.perimeter_m, ls.perimeter_m, bs.perimeter_m) AS perimeter_m
        FROM supply
        LEFT JOIN lot_via_link   ll ON true
        LEFT JOIN lot_via_spatial ls ON ll.id IS NULL
        LEFT JOIN block_via_spatial bs ON ll.id IS NULL AND ls.id IS NULL
        LIMIT 1
        """,
        [code],
    )
    if not rows or (rows[0].get("area_m2") is None and rows[0].get("perimeter_m") is None):
        raise HTTPException(status_code=404, detail="No se encontraron datos catastrales para este suministro.")
    row = rows[0]
    return {
        "supplyCode": code,
        "blockCode": row.get("block_code"),
        "lotCode": row.get("lot_code"),
        "areaM2": row.get("area_m2"),
        "perimeterM": row.get("perimeter_m"),
    }
