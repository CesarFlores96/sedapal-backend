from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_pool
from app.repositories.diameters import diameter_exists
from app.repositories.network import create_network_pipe, fetch_network_pipes


class NetworkPipeRequest(BaseModel):
    condition: str = Field(default="bueno", min_length=1, max_length=60)
    diameterMm: int | None = None
    endLat: float = Field(ge=-90, le=90)
    endLng: float = Field(ge=-180, le=180)
    material: str = Field(default="PVC", min_length=1, max_length=60)
    networkLevel: str = Field(default="secundaria", min_length=1, max_length=60)
    networkType: str = Field(min_length=1, max_length=60)
    notes: str | None = None
    startLat: float = Field(ge=-90, le=90)
    startLng: float = Field(ge=-180, le=180)


router = APIRouter(prefix="/network", tags=["network"])


@router.get("/pipes")
async def get_network_pipes(type: str | None = Query(default=None)) -> list[dict]:
    return await fetch_network_pipes(get_pool(), type.strip() if type else None)


@router.post("/pipes", status_code=201)
async def post_network_pipe(payload: NetworkPipeRequest = Body(...)) -> dict:
    if payload.diameterMm is not None and not await diameter_exists(get_pool(), payload.diameterMm):
        raise HTTPException(status_code=400, detail="El diametro seleccionado no existe en el catalogo.")

    return await create_network_pipe(
        get_pool(),
        condition=payload.condition,
        diameter_mm=payload.diameterMm,
        end_lat=payload.endLat,
        end_lng=payload.endLng,
        material=payload.material,
        network_level=payload.networkLevel,
        network_type=payload.networkType,
        notes=payload.notes.strip() if payload.notes else None,
        start_lat=payload.startLat,
        start_lng=payload.startLng,
    )
