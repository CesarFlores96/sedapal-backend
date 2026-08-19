from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_pool
from app.pagination import validate_page, validate_page_size
from app.repositories.reports import (
    fetch_consumption_anomalies,
    fetch_consumption_patterns,
    fetch_report_supply_anomalies,
    fetch_report_supply_meters,
)
from app.services.reports import get_report_master_page


router = APIRouter(prefix="/reportes", tags=["reportes"])


def ensure_period_order(start_period: str, end_period: str, label: str) -> None:
    if start_period > end_period:
        raise HTTPException(status_code=400, detail=f"El rango de {label} no es valido.")


@router.get("/master")
async def get_report_master(
    page: int = Depends(validate_page),
    page_size: int = Depends(validate_page_size),
    search: str = Query(default=""),
    consumption_filter_active: bool = Query(default=False),
    baseline_volume_m3: int | None = Query(default=None, ge=0),
    baseline_end_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    baseline_start_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    drop_m3_max: int | None = Query(default=None, ge=0),
    drop_m3_min: int | None = Query(default=None, ge=0),
    drop_percent_max: int | None = Query(default=None, ge=0),
    drop_percent_min: int | None = Query(default=None, ge=0),
    match_mode: str | None = Query(default=None, pattern="^(abrupt|decline|either)$"),
    min_var_m3: int | None = Query(default=None, ge=0),
    period_mode: str | None = Query(default=None, pattern="^(month|year)$"),
    trend_direction: str | None = Query(default=None, pattern="^(increasing|decreasing|either)$"),
    target_end_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    target_start_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    consumption_filter = None

    if (
        consumption_filter_active
        and match_mode
        and period_mode
        and trend_direction
        and target_start_period
        and target_end_period
        and baseline_start_period
        and baseline_end_period
    ):
        ensure_period_order(target_start_period, target_end_period, "objetivo")
        ensure_period_order(baseline_start_period, baseline_end_period, "base")
        consumption_filter = {
            "baselineVolumeM3": baseline_volume_m3 or 0,
            "baselineEndPeriod": baseline_end_period,
            "baselineStartPeriod": baseline_start_period,
            "dropM3Max": drop_m3_max,
            "dropM3Min": drop_m3_min if drop_m3_min is not None else 50,
            "dropPercentMax": drop_percent_max,
            "dropPercentMin": drop_percent_min or 0,
            "matchMode": match_mode,
            "minVarM3": min_var_m3 if min_var_m3 is not None else (drop_m3_min if drop_m3_min is not None else 50),
            "periodMode": period_mode,
            "trendDirection": trend_direction,
            "targetEndPeriod": target_end_period,
            "targetStartPeriod": target_start_period,
        }

    return await get_report_master_page(
        pool=get_pool(),
        page=page,
        page_size=page_size,
        search=search.strip(),
        consumption_filter=consumption_filter,
    )


@router.get("/consumption-anomalies")
async def get_consumption_anomalies(
    baselineEndPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    baselineStartPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    baselineVolumeM3: int = Query(default=100, ge=0),
    dropM3Max: int | None = Query(default=None, ge=0),
    dropM3Min: int = Query(default=50, ge=0),
    dropPercentMax: int | None = Query(default=None, ge=0),
    dropPercentMin: int = Query(default=30, ge=0),
    matchMode: str = Query(default="decline", pattern="^(abrupt|decline|either)$"),
    minVarM3: int | None = Query(default=None, ge=0),
    periodMode: str = Query(default="month", pattern="^(month|year)$"),
    targetEndPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    targetStartPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    trendDirection: str = Query(default="either", pattern="^(increasing|decreasing|either)$"),
) -> dict:
    ensure_period_order(targetStartPeriod, targetEndPeriod, "objetivo")
    ensure_period_order(baselineStartPeriod, baselineEndPeriod, "base")

    return await fetch_consumption_anomalies(
        get_pool(),
        baseline_end_period=baselineEndPeriod,
        baseline_start_period=baselineStartPeriod,
        baseline_volume_m3=baselineVolumeM3,
        drop_m3_max=dropM3Max,
        min_var_m3=minVarM3 if minVarM3 is not None else dropM3Min,
        drop_percent_max=dropPercentMax,
        drop_percent_min=dropPercentMin,
        match_mode=matchMode,
        period_mode=periodMode,
        target_end_period=targetEndPeriod,
        target_start_period=targetStartPeriod,
        trend_direction=trendDirection,
    )


@router.get("/meters")
async def get_report_supply_meters(
    supply_code: str = Query(..., min_length=1),
) -> dict:
    return {
        "data": await fetch_report_supply_meters(
            get_pool(),
            supply_code=supply_code.strip(),
        )
    }


@router.get("/supply-anomalies")
async def get_report_supply_anomalies(
    supply_code: str = Query(..., min_length=1),
) -> dict:
    return {
        "data": await fetch_report_supply_anomalies(
            get_pool(),
            supply_code=supply_code.strip(),
        )
    }


@router.get("/consumption-patterns")
async def get_consumption_patterns(
    startPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    endPeriod: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    baselineVolumeM3: int = Query(default=100, ge=0),
) -> dict:
    if startPeriod > endPeriod:
        raise HTTPException(status_code=400, detail="El rango de meses no es valido.")

    start_year, start_month = [int(part) for part in startPeriod.split("-", 1)]
    end_year, end_month = [int(part) for part in endPeriod.split("-", 1)]
    total_months = (end_year - start_year) * 12 + (end_month - start_month) + 1

    if total_months < 3:
        raise HTTPException(
            status_code=400,
            detail="Selecciona al menos tres meses para evaluar el patron.",
        )

    return await fetch_consumption_patterns(
        get_pool(),
        start_period=startPeriod,
        end_period=endPeriod,
        baseline_volume_m3=baselineVolumeM3,
    )
