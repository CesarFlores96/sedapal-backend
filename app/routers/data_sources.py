from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.services.data_sources import (
    get_contrastations_page,
    get_data_sources_overview,
    get_meter_installations_page,
    get_service_orders_page,
)


router = APIRouter(prefix="/data-sources", tags=["data-sources"])


@router.get("/overview")
async def overview() -> dict[str, object]:
    return await get_data_sources_overview(pool=get_pool())


@router.get("/contrastations", response_model=PaginatedDataResponse)
async def list_contrastations(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str | None = Query(default=None),
) -> PaginatedDataResponse:
    return await get_contrastations_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        search=search.strip() if search else None,
    )


@router.get("/meter-installations", response_model=PaginatedDataResponse)
async def list_meter_installations(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str | None = Query(default=None),
) -> PaginatedDataResponse:
    return await get_meter_installations_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        search=search.strip() if search else None,
    )


@router.get("/service-orders", response_model=PaginatedDataResponse)
async def list_service_orders(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str | None = Query(default=None),
) -> PaginatedDataResponse:
    return await get_service_orders_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        search=search.strip() if search else None,
    )
