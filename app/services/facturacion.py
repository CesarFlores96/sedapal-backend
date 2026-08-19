from psycopg_pool import AsyncConnectionPool

from app.repositories.facturacion import fetch_facturacion
from app.schemas.facturacion import FacturacionResponse
from app.services.shared import build_paginated_response


async def get_facturacion_page(
    pool: AsyncConnectionPool,
    suministro: str,
    page: int,
    page_size: int,
) -> FacturacionResponse:
    data = await fetch_facturacion(pool=pool, suministro=suministro, page=page, page_size=page_size)
    return FacturacionResponse.model_validate(
        build_paginated_response(page=page, page_size=page_size, data=data).model_dump()
    )
