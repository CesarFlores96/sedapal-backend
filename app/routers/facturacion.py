from fastapi import APIRouter, Depends, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.schemas.facturacion import FacturacionResponse
from app.services.facturacion import get_facturacion_page


router = APIRouter(prefix="/facturacion", tags=["facturacion"])


@router.get("", response_model=FacturacionResponse)
async def list_facturacion(
    suministro: str = Query(..., min_length=1, max_length=64, description="Codigo de suministro"),
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
) -> FacturacionResponse:
    return await get_facturacion_page(
        pool=get_pool(),
        suministro=suministro.strip(),
        page=page,
        page_size=page_size,
    )
