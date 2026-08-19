from fastapi import APIRouter

from app.database import get_pool
from app.repositories.dashboard import fetch_dashboard_data


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard(tab: str | None = None) -> dict:
    return await fetch_dashboard_data(get_pool(), tab=tab)
