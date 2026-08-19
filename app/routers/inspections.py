from fastapi import APIRouter, Depends, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.services.inspections import get_inspections_page


router = APIRouter(prefix="/inspections", tags=["inspections"])


@router.get("", response_model=PaginatedDataResponse)
async def list_inspections(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    supply_code: str | None = Query(default=None, min_length=1, max_length=64),
    search: str | None = Query(default=None),
) -> PaginatedDataResponse:
    return await get_inspections_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        supply_code=supply_code.strip() if supply_code else None,
        search=search.strip() if search else None,
    )
