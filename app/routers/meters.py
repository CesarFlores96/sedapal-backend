from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from pydantic import BaseModel

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.services.meters import (
    get_meter_by_supply_code,
    get_meter_planning_stats,
    get_meter_registry_page,
    get_meter_registry_stats,
    get_meter_vencimiento_rows,
    get_meters_page,
    update_meter_fields,
)


router = APIRouter(prefix="/meters", tags=["meters"])


class MeterUpdateRequest(BaseModel):
    meter_serial: str | None = None
    diameter_mm: int | None = None
    installation_date: str | None = None
    supply_status: str | None = None
    segment_name: str | None = None
    meter_status: str | None = None


@router.get("", response_model=PaginatedDataResponse)
async def list_meters(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str = "",
    status_filter: str | None = None,
) -> PaginatedDataResponse:
    return await get_meters_page(
        pool=get_pool(), page=page, page_size=page_size, search=search, status=status_filter
    )


@router.get("/planning-stats")
async def get_planning_stats() -> dict:
    return await get_meter_planning_stats(get_pool())


@router.patch("/{supply_code}")
async def patch_meter(
    supply_code: str = Path(..., min_length=1, max_length=80),
    payload: MeterUpdateRequest = Body(...),
) -> dict:
    if not supply_code.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El codigo de suministro es requerido.")

    await update_meter_fields(
        get_pool(),
        supply_code=supply_code.strip(),
        meter_serial=payload.meter_serial,
        diameter_mm=payload.diameter_mm,
        installation_date=payload.installation_date,
        supply_status=payload.supply_status,
        segment_name=payload.segment_name,
        meter_status=payload.meter_status,
    )
    return {"success": True}


@router.get("/registry", response_model=PaginatedDataResponse)
async def list_meter_registry(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str = "",
    registry_status: str | None = None,
) -> PaginatedDataResponse:
    return await get_meter_registry_page(
        pool=get_pool(), page=page, page_size=page_size, registry_status=registry_status, search=search
    )


@router.get("/registry/stats")
async def get_registry_stats() -> list[dict]:
    return await get_meter_registry_stats(get_pool())


@router.get("/by-supply-code/{supply_code}")
async def get_meter_detail(
    supply_code: str = Path(..., min_length=1, max_length=80),
) -> dict:
    data = await get_meter_by_supply_code(get_pool(), supply_code.strip())
    return {"data": data}


@router.get("/vencimiento/{status_value}")
async def get_vencimiento_rows(
    status_value: Literal["vencido", "por_vencer", "vigente", "sin_datos"],
) -> dict:
    data = await get_meter_vencimiento_rows(get_pool(), status_value)
    return {"data": data}
