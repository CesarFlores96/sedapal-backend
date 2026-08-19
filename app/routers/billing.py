from fastapi import APIRouter, Depends, Path, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.schemas.consumption_analysis import ConsumptionAnalysisResponse
from app.services.billing import (
    get_billing_customers_page,
    get_billing_detail_page,
    get_consumption_analysis,
)
from app.services.consumption_analysis import MIN_OBSERVATIONS_DEFAULT


router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/{supply_code}/analysis", response_model=ConsumptionAnalysisResponse)
async def get_billing_analysis(
    supply_code: str = Path(..., min_length=1, max_length=64),
    min_obs: int = Query(MIN_OBSERVATIONS_DEFAULT, ge=1, le=24),
) -> ConsumptionAnalysisResponse:
    return await get_consumption_analysis(
        pool=get_pool(),
        supply_code=supply_code.strip(),
        min_obs=min_obs,
    )


@router.get("/customers", response_model=PaginatedDataResponse)
async def list_billing_customers(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
) -> PaginatedDataResponse:
    return await get_billing_customers_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
    )


@router.get("/{supply_code}", response_model=PaginatedDataResponse)
async def get_billing_detail(
    supply_code: str = Path(..., min_length=1, max_length=64),
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
) -> PaginatedDataResponse:
    return await get_billing_detail_page(
        pool=get_pool(),
        supply_code=supply_code.strip(),
        page=page,
        page_size=page_size,
    )
