from typing import Any

from pydantic import BaseModel, Field


class PaginatedDataResponse(BaseModel):
    success: bool = True
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=500)
    total_returned: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    data: list[dict[str, Any]]
