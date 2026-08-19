import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import get_settings
from app.database import get_cadastral_pool, get_gis_pool, get_martin_pool
from app.repositories.customer_supplies import search_supplies_vectorial
from app.services.embeddings import get_embedding
from app.sedapalgis.repositories.gis import (
    fetch_district_catalog,
    fetch_corrected_lot_tile,
    fetch_layers,
    fetch_lot_context,
    fetch_supply_consumption,
    fetch_supply_detail,
    resolve_location,
    save_geometry_correction,
    search_cadastre,
)
from app.sedapalgis.repositories.reportes import (
    build_client_lot_analysis,
    build_supply_analysis,
    fetch_abrupt_consumption_drops,
    fetch_report_header,
    fetch_report_master_page,
    fetch_supply_details,
    fetch_supply_indicators,
)
from app.sedapalgis.schemas import GeometryCorrectionRequest, parse_bbox, parse_layers


gis_router = APIRouter(prefix="/v1/gis", tags=["sedapalgis"])
reports_router = APIRouter(prefix="/v1/reportes", tags=["sedapalgis-reportes"])
updater_router = APIRouter(prefix="/updater", tags=["sedapalgis-updater"])

EVOLUTION_ROW_KEYS = (
    "year", "month", "label", "currentVolume", "previousVolume", "historicalMedian",
    "variationVsMedianPercent", "variationVsPreviousYearPercent", "previousYearDifference",
    "absoluteDifference", "isAnomaly", "severity", "type", "baselineYears",
    "baselineValues", "baselineSampleCount", "bySupply",
)
SUMMARY_KEYS = (
    "accumulatedVolume", "historicalAccumulatedMedian", "medianDeltaPercent", "medianDeltaM3",
    "previousDeltaPercent", "previousDeltaM3", "baselineStartYear", "baselineEndPeriod",
)
ANALYSIS_DETAIL_KEYS = ("severity", "score", "robustZScore", "reasons")
INSIGHT_CARD_KEYS = ("title", "description", "tone")
MVT_SOURCES = {
    "mvt.blocks": "blocks",
    "mvt.districts": "districts",
    "mvt.lots": "lots",
    "mvt.supplies": "supplies",
    "mvt.valves": "valves",
    "mvt.water_connections": "water_connections",
    "mvt.water_pipes": "water_pipes",
}


def _pick(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: source.get(key) for key in keys}


def _shape_year(year_payload: dict) -> dict:
    return {
        "evolutionRows": [_pick(row, EVOLUTION_ROW_KEYS) for row in year_payload.get("evolutionRows", [])],
        "summary": _pick(year_payload.get("summary", {}), SUMMARY_KEYS),
        "insightCards": [_pick(card, INSIGHT_CARD_KEYS) for card in year_payload.get("insightCards", [])],
        "analysis": _pick(year_payload.get("analysis", {}), ANALYSIS_DETAIL_KEYS),
    }


def _build_consumption(row: dict) -> dict | None:
    supply_avg = row.get("supply_avg_m3")
    district_avg = row.get("district_avg_m3")
    if supply_avg is None and district_avg is None:
        return None
    comparison_percent = None
    if supply_avg is not None and district_avg is not None and district_avg > 0:
        comparison_percent = ((supply_avg - district_avg) / district_avg) * 100
    return {
        "currentYearAverageM3": supply_avg,
        "readingCount": row.get("supply_reading_count") or 0,
        "districtAverageM3": district_avg,
        "districtSupplyCount": row.get("district_supply_count") or 0,
        "comparisonPercent": comparison_percent,
    }


def _tile_secret() -> str:
    settings = get_settings()
    return settings.supabase_jwt_secret or settings.api_key


def _tile_session_token() -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode({"scope": "tiles", "iat": now, "exp": now + 600}, _tile_secret(), algorithm="HS256")


def _verify_tile_session(token: str) -> None:
    try:
        payload = jwt.decode(token, _tile_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Sesion de tiles invalida o expirada.") from None
    if payload.get("scope") != "tiles":
        raise HTTPException(status_code=401, detail="Sesion de tiles invalida.")


@gis_router.get("/distritos")
async def district_catalog() -> dict:
    return {"districts": await fetch_district_catalog(get_gis_pool())}


@gis_router.get("/catastro/buscar")
async def cadastral_search(
    query: str = Query(..., min_length=2, max_length=40),
    kind: str = Query(default="all", pattern="^(all|block|lot)$"),
    limit: int = Query(default=12, ge=1, le=30),
) -> dict:
    return {"results": await search_cadastre(get_gis_pool(), query, kind, limit)}


@gis_router.post("/catastro/ajuste")
async def adjust_cadastral_geometry(body: GeometryCorrectionRequest, request: Request) -> dict:
    result = await save_geometry_correction(
        get_gis_pool(), body.targetKind, str(body.targetId), body.deltaLng, body.deltaLat,
        request.headers.get("x-auth-user-id"), body.reset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="La geometria catastral no existe.")
    return result


@gis_router.get("/capas")
async def layers(
    bbox: str = Query(...),
    layers: str = Query(default="distritos,suministros,medidores"),
    district: str | None = Query(default=None, min_length=1, max_length=120),
    zoom: float = Query(default=10, ge=0, le=24),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=500, ge=1, le=2000),
) -> dict:
    try:
        parsed_bbox = parse_bbox(bbox)
        parsed_layers = parse_layers(layers)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await fetch_layers(
        get_gis_pool(), parsed_bbox, parsed_layers, page, page_size, district=district, zoom=zoom
    )


@gis_router.get("/suministro/{supply_code}")
async def supply_detail(supply_code: str) -> dict:
    row = await fetch_supply_detail(get_gis_pool(), supply_code.strip())
    if not row:
        raise HTTPException(status_code=404, detail="Suministro no encontrado.")
    geometry = row.get("geometry")
    if not geometry and row.get("longitude") is not None and row.get("latitude") is not None:
        geometry = {"type": "Point", "coordinates": [float(row["longitude"]), float(row["latitude"])]}
    geometry_available = bool(isinstance(geometry, dict) and geometry.get("type") == "Point")
    return {
        "supply": {
            "id": row["id"], "code": row["supply_code"], "customerName": row.get("customer_name"),
            "address": row.get("service_address"), "status": row.get("supply_status"),
            "locationSource": row.get("location_source"), "locationQuality": row.get("location_quality"),
        },
        "geometry": geometry,
        "meter": ({
            "code": row.get("meter_code"), "diameter": row.get("meter_diameter"),
            "installationDate": row.get("installation_date"), "status": row.get("meter_status"),
        } if row.get("meter_code") else None),
        "hierarchy": {
            "district": row.get("structured_district_name") or row.get("district") or row.get("district_text"),
            "quadrant": row.get("sector"), "lot": row.get("structured_cup_code") or row.get("lot_code"),
            "provisional": not geometry_available, "geometryAvailable": geometry_available,
        },
        "cadastre": ({
            "districtCode": row.get("structured_district_code"),
            "districtName": row.get("structured_district_name"),
            "districtMatchStatus": row.get("district_match_status"),
            "blockCode": row.get("structured_block_code"), "cupCode": row.get("structured_cup_code"),
            "geometryMatchStatus": row.get("geometry_match_status"),
            "geometryCount": row.get("gis_lot_match_count") or 0, "cuaCode": row.get("cua_code"),
            "cuaLabel": row.get("cua_label"), "cuaCatalogDescription": row.get("cua_catalog_description"),
            "cuaMatchMethod": row.get("cua_match_method"),
        } if row.get("structured_district_code") or row.get("structured_cup_code") or row.get("cua_label") else None),
        "cadastralLink": ({
            "kind": "lot", "recordId": row["resolved_lot_id"], "code": row.get("resolved_lot_code"),
            "blockCode": row.get("resolved_block_code"), "method": row.get("resolved_lot_method"),
        } if row.get("resolved_lot_id") else None),
        "consumption": None,
        "consumptionLoading": True,
    }


@gis_router.get("/suministro/{supply_code}/consumo")
async def supply_consumption(supply_code: str) -> dict | None:
    row = await fetch_supply_consumption(get_gis_pool(), supply_code.strip())
    if row is None:
        raise HTTPException(status_code=404, detail="Suministro no encontrado.")
    return _build_consumption(row)


@gis_router.get("/relacion")
async def relationship(
    lng: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    tolerance_m: float = Query(default=25, gt=0, le=500),
) -> dict:
    return await resolve_location(get_gis_pool(), lng, lat, tolerance_m)


@gis_router.get("/lote/{lot_id}")
async def lot_context(lot_id: str) -> dict:
    result = await fetch_lot_context(get_gis_pool(), lot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Lote no encontrado.")
    return result


@gis_router.post("/tiles/session")
async def tile_session(request: Request) -> dict:
    token = _tile_session_token()
    base = str(request.base_url).rstrip("/")
    return {"tileBaseUrl": f"{base}/api/v1/gis/tiles/{token}", "expiresIn": 600}


@gis_router.get("/tiles/{session_token}/{source}/{z}/{x}/{y}")
async def vector_tile(session_token: str, source: str, z: int, x: int, y: int) -> Response:
    _verify_tile_session(session_token)
    function_name = MVT_SOURCES.get(source)
    if function_name is None:
        raise HTTPException(status_code=404, detail="Fuente MVT no encontrada.")
    if not (0 <= z <= 24 and x >= 0 and y >= 0):
        raise HTTPException(status_code=422, detail="Coordenadas de tile invalidas.")
    if source == "mvt.lots":
        payload = await fetch_corrected_lot_tile(get_cadastral_pool(), z, x, y)
        return Response(payload, media_type="application/vnd.mapbox-vector-tile")
    async with get_martin_pool().connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(f'SELECT mvt."{function_name}"(%s, %s, %s)', (z, x, y))
            row = await cursor.fetchone()
    payload = bytes(row[0]) if row and row[0] is not None else b""
    return Response(payload, media_type="application/vnd.mapbox-vector-tile")


@reports_router.get("/master")
async def report_master(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
    search: str = Query(default="", max_length=160), filter_active: bool = Query(default=False),
    trend_direction: str = Query(default="either", pattern="^(increasing|decreasing|either)$"),
    min_trend_percent: float = Query(default=0, ge=0, le=10000),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    baseline_start_period: str = Query(pattern=r"^\d{4}-\d{2}$"),
    baseline_end_period: str = Query(pattern=r"^\d{4}-\d{2}$"),
    target_start_period: str = Query(pattern=r"^\d{4}-\d{2}$"),
    target_end_period: str = Query(pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    if baseline_start_period > baseline_end_period or target_start_period > target_end_period:
        raise HTTPException(status_code=400, detail="El rango de consumo no es valido.")
    return await fetch_report_master_page(
        get_gis_pool(), page=page, page_size=page_size, search=search.strip(),
        filter_active=filter_active, trend_direction=trend_direction,
        min_trend_percent=min_trend_percent, sort_order=sort_order,
        baseline_start_period=baseline_start_period, baseline_end_period=baseline_end_period,
        target_start_period=target_start_period, target_end_period=target_end_period,
    )


@reports_router.get("/anomalias/caidas-consumo")
async def abrupt_consumption_drops(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    classification: str | None = Query(default=None, pattern="^(grandes_clientes|fuente_propia|operativo)$"),
    kind: str | None = Query(default=None, pattern="^(zero|extremely_low)$"),
    district: str = Query(default="", max_length=100),
    analysis_scope: str = Query(default="supply", pattern="^(supply|property)$"),
    search: str = Query(default="", max_length=160),
) -> dict:
    normalized_search = search.strip()
    vector_codes: list[str] = []
    if len(normalized_search) >= 3 and not normalized_search.isdigit():
        try:
            query_embedding = await asyncio.to_thread(get_embedding, normalized_search)
            vector_rows = await search_supplies_vectorial(
                get_gis_pool(), query_embedding=query_embedding, limit=100
            )
            vector_codes = [str(row["supply_code"]) for row in vector_rows]
        except Exception:
            # La coincidencia textual sigue disponible si el indice semantico
            # aun no fue generado o el modelo local no puede inicializarse.
            vector_codes = []
    return await fetch_abrupt_consumption_drops(
        get_gis_pool(),
        page=page,
        page_size=page_size,
        classification=classification,
        kind=kind,
        district=district.strip(),
        analysis_scope=analysis_scope,
        search=normalized_search,
        vector_supply_codes=vector_codes,
    )


@reports_router.get("/suministro/{supply_code}/header")
async def supply_report_header(supply_code: str) -> dict:
    normalized = supply_code.strip()
    header = await fetch_report_header(get_gis_pool(), normalized)
    if not header:
        raise HTTPException(status_code=404, detail="Suministro no encontrado.")
    return header


@reports_router.get("/cliente-lote/reporte")
async def client_lot_report(supply_codes: list[str] = Query(...)) -> dict:
    try:
        analysis = await build_client_lot_analysis(get_gis_pool(), supply_codes)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "supplyCode": analysis.get("supplyCode", analysis["group"]["propertyCode"]),
        "years": analysis.get("years", []),
        "header": analysis["header"],
        "analysisByYear": {
            year: _shape_year(value) for year, value in analysis.get("analysisByYear", {}).items()
        },
        "group": analysis["group"],
        "details": analysis.get("details"),
        "generatedAt": analysis.get("generatedAt"),
    }


@reports_router.get("/suministro/{supply_code}/spatial")
async def supply_report_spatial(supply_code: str) -> dict:
    return await fetch_supply_indicators(get_gis_pool(), supply_code.strip())


@reports_router.get("/suministro/{supply_code}/details")
async def supply_report_details(supply_code: str) -> dict:
    return await fetch_supply_details(get_gis_pool(), supply_code.strip())


@reports_router.get("/suministro/{supply_code}/temporal")
async def supply_report_temporal(supply_code: str) -> dict:
    normalized = supply_code.strip()
    analysis = await build_supply_analysis(get_gis_pool(), normalized)
    return {
        "supplyCode": analysis.get("supplyCode", normalized),
        "years": analysis.get("years", []),
        "analysisByYear": {
            year: _shape_year(value) for year, value in analysis.get("analysisByYear", {}).items()
        },
        "billing": analysis.get("billingRows", []),
        "generatedAt": analysis.get("generatedAt"),
    }


@reports_router.get("/suministro/{supply_code}")
async def supply_report(supply_code: str) -> dict:
    normalized = supply_code.strip()
    pool = get_gis_pool()
    header, analysis, indicators, details = await asyncio.gather(
        fetch_report_header(pool, normalized), build_supply_analysis(pool, normalized),
        fetch_supply_indicators(pool, normalized), fetch_supply_details(pool, normalized),
    )
    if not header:
        raise HTTPException(status_code=404, detail="Suministro no encontrado.")
    return {
        "supplyCode": analysis.get("supplyCode", normalized), "years": analysis.get("years", []),
        "header": header,
        "analysisByYear": {year: _shape_year(value) for year, value in analysis.get("analysisByYear", {}).items()},
        "indicators": indicators, "details": {**details, "billing": analysis.pop("billingRows", [])},
        "generatedAt": analysis.get("generatedAt"),
    }


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except ValueError:
        return (0,)


@updater_router.get("/{target}/{arch}/{current_version}")
async def check_for_update(target: str, arch: str, current_version: str) -> Response:
    settings = get_settings()
    manifest = Path(settings.updater_releases_dir) / "latest.json"
    if not manifest.is_file():
        return Response(status_code=204)
    release = json.loads(manifest.read_text(encoding="utf-8"))
    if _version_tuple(release["version"]) <= _version_tuple(current_version):
        return Response(status_code=204)
    platform = release.get("platforms", {}).get(f"{target}-{arch}")
    if not platform:
        return Response(status_code=204)
    return Response(
        content=json.dumps({
            "version": release["version"], "notes": release.get("notes", ""),
            "pub_date": release["pub_date"],
            "url": f"{settings.updater_public_base_url}/updater/files/{platform['file']}",
            "signature": platform["signature"],
        }),
        media_type="application/json",
    )
