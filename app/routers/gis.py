from fastapi import APIRouter, Query

from app.database import get_pool
from app.repositories.gis import fetch_operational_gis_data, fetch_anomalies_gis_data


router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/operational")
async def get_operational_gis(
    min_lat: float | None = Query(default=None),
    max_lat: float | None = Query(default=None),
    min_lng: float | None = Query(default=None),
    max_lng: float | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=500, ge=1, le=2000),
    include_pipes: bool = Query(default=False),
) -> dict:
    return await fetch_operational_gis_data(
        get_pool(),
        min_lat=min_lat,
        max_lat=max_lat,
        min_lng=min_lng,
        max_lng=max_lng,
        page=page,
        page_size=page_size,
        include_pipes=include_pipes,
    )


@router.get("/anomalies")
async def get_anomalies_gis() -> dict:
    return await fetch_anomalies_gis_data(get_pool())
