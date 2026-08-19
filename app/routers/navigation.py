from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings


router = APIRouter(prefix="/navigation", tags=["navigation"])


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteStop(GeoPoint):
    id: str = Field(min_length=1, max_length=120)


class NavigationRouteRequest(BaseModel):
    origin: GeoPoint
    stops: list[RouteStop] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distinct_stops(self) -> "NavigationRouteRequest":
        if len({stop.id for stop in self.stops}) != len(self.stops):
            raise ValueError("Las paradas de navegacion no pueden repetirse.")
        return self


def _maneuver_instruction(step: dict) -> str:
    maneuver = step.get("maneuver") or {}
    kind = str(maneuver.get("type") or "continue")
    modifier = str(maneuver.get("modifier") or "").strip()
    road_name = str(step.get("name") or "").strip()
    action = {
        "arrive": "Llegaste al destino",
        "depart": "Inicia la ruta",
        "end of road": "Al final de la vía",
        "merge": "Incorpórate",
        "new name": "Continúa",
        "notification": "Continúa",
        "off ramp": "Toma la salida",
        "on ramp": "Incorpórate",
        "roundabout": "En la rotonda",
        "rotary": "En la rotonda",
        "turn": "Gira",
        "use lane": "Usa el carril indicado",
    }.get(kind, "Continúa")
    direction = {
        "left": " a la izquierda",
        "right": " a la derecha",
        "sharp left": " pronunciadamente a la izquierda",
        "sharp right": " pronunciadamente a la derecha",
        "slight left": " levemente a la izquierda",
        "slight right": " levemente a la derecha",
        "straight": " recto",
        "uturn": " y retorna",
    }.get(modifier, "")
    onto = f" por {road_name}" if road_name else ""
    return f"{action}{direction}{onto}".strip()


def normalize_osrm_route(payload: dict) -> dict:
    routes = payload.get("routes") or []
    route = routes[0] if routes else None
    geometry = route.get("geometry", {}).get("coordinates") if isinstance(route, dict) else None
    if payload.get("code") != "Ok" or not isinstance(geometry, list) or len(geometry) < 2:
        raise ValueError("OSRM no devolvio una ruta transitable.")

    legs: list[dict] = []
    for leg_index, leg in enumerate(route.get("legs") or []):
        steps: list[dict] = []
        for step_index, step in enumerate(leg.get("steps") or []):
            maneuver = step.get("maneuver") or {}
            location = maneuver.get("location") or [None, None]
            if not isinstance(location, list) or len(location) < 2:
                continue
            steps.append({
                "index": step_index,
                "distanceMeters": round(float(step.get("distance") or 0)),
                "durationSeconds": round(float(step.get("duration") or 0)),
                "instruction": _maneuver_instruction(step),
                "location": {"longitude": location[0], "latitude": location[1]},
                "modifier": maneuver.get("modifier"),
                "type": maneuver.get("type") or "continue",
            })
        legs.append({
            "index": leg_index,
            "distanceMeters": round(float(leg.get("distance") or 0)),
            "durationSeconds": round(float(leg.get("duration") or 0)),
            "steps": steps,
        })

    return {
        "geometry": {"type": "LineString", "coordinates": geometry},
        "distanceMeters": round(float(route.get("distance") or 0)),
        "durationSeconds": round(float(route.get("duration") or 0)),
        "legs": legs,
        "source": "osrm",
    }


@router.post("/route")
async def post_navigation_route(payload: NavigationRouteRequest) -> dict:
    settings = get_settings()
    if not settings.osrm_base_url:
        raise HTTPException(status_code=503, detail="El servicio de navegacion no esta configurado.")
    if len(payload.stops) > settings.osrm_max_stops:
        raise HTTPException(
            status_code=422,
            detail=f"La ruta admite hasta {settings.osrm_max_stops} paradas.",
        )

    coordinates = [payload.origin, *payload.stops]
    coordinate_path = ";".join(f"{point.longitude},{point.latitude}" for point in coordinates)
    url = f"{settings.osrm_base_url.rstrip('/')}/route/v1/driving/{coordinate_path}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.osrm_timeout_seconds, connect=5)) as client:
            response = await client.get(url, params={"overview": "full", "geometries": "geojson", "steps": "true"})
            response.raise_for_status()
            return normalize_osrm_route(response.json())
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="El servicio de navegacion no respondio a tiempo.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="No se pudo consultar el servicio de navegacion.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
