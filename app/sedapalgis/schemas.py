from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

LayerName = Literal[
    "distritos",
    "manzanas",
    "cuadrantes",
    "lotes",
    "tuberias",
    "alcantarillado",
    "suministros",
    "medidores",
]

ALLOWED_LAYERS: set[str] = {
    "distritos",
    "manzanas",
    "cuadrantes",
    "lotes",
    "tuberias",
    "alcantarillado",
    "suministros",
    "medidores",
}


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=1, max_length=4096)


class GeometryCorrectionRequest(BaseModel):
    targetKind: Literal["block", "lot"]
    targetId: UUID
    deltaLng: float = Field(default=0, ge=-0.05, le=0.05)
    deltaLat: float = Field(default=0, ge=-0.05, le=0.05)
    reset: bool = False


class BBox(BaseModel):
    minx: float = Field(ge=-180, le=180)
    miny: float = Field(ge=-90, le=90)
    maxx: float = Field(ge=-180, le=180)
    maxy: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def validate_order(self) -> "BBox":
        if self.minx >= self.maxx or self.miny >= self.maxy:
            raise ValueError("El bbox debe cumplir minx < maxx y miny < maxy.")
        return self

    def as_params(self) -> list[float]:
        return [self.minx, self.miny, self.maxx, self.maxy]


def parse_bbox(value: str) -> BBox:
    parts = value.split(",")
    if len(parts) != 4:
        raise ValueError("bbox debe tener cuatro coordenadas: minx,miny,maxx,maxy.")
    try:
        return BBox(minx=float(parts[0]), miny=float(parts[1]), maxx=float(parts[2]), maxy=float(parts[3]))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def parse_layers(value: str) -> list[str]:
    layers = list(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    invalid = sorted(set(layers) - ALLOWED_LAYERS)
    if invalid:
        raise ValueError(f"Capas no admitidas: {', '.join(invalid)}")
    if not layers:
        raise ValueError("Debe solicitar al menos una capa.")
    return layers
