from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from app.repositories.geocoding import resolve_map_link, search_address


class MapResolveRequest(BaseModel):
    mapInput: str = Field(min_length=1)


router = APIRouter(prefix="/geocode", tags=["geocode"])


@router.get("/search")
async def get_search_address(
    q: str = Query(..., min_length=1, max_length=300),
) -> dict[str, float] | None:
    return await search_address(q.strip())


@router.post("/resolve")
async def post_resolve_map_link(payload: MapResolveRequest = Body(...)) -> list[float] | None:
    coordinates = await resolve_map_link(payload.mapInput.strip())
    return list(coordinates) if coordinates else None
