from app.schemas.common import PaginatedDataResponse


def build_paginated_response(
    *,
    page: int,
    page_size: int,
    data: list[dict],
) -> PaginatedDataResponse:
    return PaginatedDataResponse(
        success=True,
        page=page,
        page_size=page_size,
        total_returned=len(data),
        data=data,
    )
