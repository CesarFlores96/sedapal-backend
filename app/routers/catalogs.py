from fastapi import APIRouter, Body, HTTPException, Path, status
from pydantic import BaseModel

from app.database import get_pool
from app.repositories.catalogs import fetch_supervision_catalogs
from app.repositories.diameters import list_diameter_catalog, update_diameter_vida_util


class DiameterVidaUtilUpdateRequest(BaseModel):
    vida_util_anios: int | None = None


router = APIRouter(prefix="/catalogs", tags=["catalogs"])


@router.get("/diameters")
async def get_diameters() -> list[dict]:
    return await list_diameter_catalog(get_pool())


@router.patch("/diameters/{diameter_mm}")
async def patch_diameter_vida_util(
    diameter_mm: int = Path(..., gt=0),
    payload: DiameterVidaUtilUpdateRequest = Body(...),
) -> dict:
    updated = await update_diameter_vida_util(get_pool(), diameter_mm, payload.vida_util_anios)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Diametro no encontrado.")
    return {"success": True}


@router.get("/supervision-codes")
async def get_supervision_codes() -> dict[str, dict[int, dict[str, str]]]:
    return {"catalogs": await fetch_supervision_catalogs(get_pool())}
