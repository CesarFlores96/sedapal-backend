from fastapi import APIRouter, Depends, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.services.customers import get_customers_page


router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("", response_model=PaginatedDataResponse)
async def list_customers(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str | None = Query(default=None, min_length=1, max_length=120),
) -> PaginatedDataResponse:
    return await get_customers_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        search=search.strip() if search else None,
    )
