from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.common import PaginatedDataResponse
from app.services.readings import get_readings_page
from app.repositories.shared import fetch_all_dict


router = APIRouter(prefix="/readings", tags=["readings"])


class ReadingCodesRequest(BaseModel):
    supplyCodes: list[str] = Field(default_factory=list, max_length=2000)


@router.get("", response_model=PaginatedDataResponse)
async def list_readings(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    supply_code: str | None = Query(default=None, min_length=1, max_length=64),
    search: str | None = Query(default=None),
) -> PaginatedDataResponse:
    return await get_readings_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        supply_code=supply_code.strip() if supply_code else None,
        search=search.strip() if search else None,
    )


@router.post("/latest/batch")
async def latest_readings(payload: ReadingCodesRequest = Body(...)) -> dict:
    codes = list(dict.fromkeys(code.strip() for code in payload.supplyCodes if code.strip()))[:2000]
    if not codes:
        return {"data": []}
    rows = await fetch_all_dict(
        get_pool(),
        """
        SELECT DISTINCT ON (supply_code)
          supply_code, reading_date::text AS reading_date,
          COALESCE(current_reading::text, '') AS current_reading
        FROM public.customer_supply_readings
        WHERE supply_code = ANY(%s)
        ORDER BY supply_code, reading_date DESC NULLS LAST, reading_time DESC NULLS LAST
        """,
        [codes],
    )
    return {"data": rows}
