import asyncio
import json
from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool

from app.sedapalgis.repositories.shared import fetch_all, fetch_one
from app.sedapalgis.schemas import BBox

MAX_MANUAL_CORRECTION_METERS = 15.0

# Margen para dos correcciones acumuladas (manzana + lote), cada una limitada
# a 15 m. En Lima, 0.0003 grados cubre ese desplazamiento con holgura sin
# ampliar cada viewport varios kilometros antes de aplicar el indice GIST.
MAX_CORRECTION_DEGREES = 0.0003

# Precision de salida de ST_AsGeoJSON. 6 decimales son ~11 cm en Lima; PostGIS
# emite 9 por defecto, lo que triplica el payload sin aportar precision util.
GEOJSON_DECIMALS = 6


async def fetch_corrected_lot_tile(
    pool: AsyncConnectionPool, z: int, x: int, y: int
) -> bytes:
    """Genera lotes MVT heredando la correccion de su manzana y la propia."""
    row = await fetch_one(
        pool,
        """
        WITH bounds AS (
          SELECT ST_TileEnvelope(%s, %s, %s) AS geom_3857
        ), corrected AS (
          SELECT
            l.id,
            l.block_id,
            l.district_id,
            l.lot_code,
            l.cup_code,
            l.cod_mza,
            l.property_code,
            l.locality_code,
            l.lot_type_code,
            l.project_status,
            l.levels,
            l.block_match_method,
            l.source,
            l.geom,
            ST_Transform(
              ST_Translate(
                ST_Transform(l.geom, 4326),
                COALESCE(block_correction.delta_lng, 0) + COALESCE(lot_correction.delta_lng, 0),
                COALESCE(block_correction.delta_lat, 0) + COALESCE(lot_correction.delta_lat, 0)
              ),
              3857
            ) AS geom_3857
          FROM public.gis_lots l
          LEFT JOIN public.gis_geometry_corrections block_correction
            ON block_correction.target_kind = 'block'
           AND block_correction.block_id = l.block_id
          LEFT JOIN public.gis_geometry_corrections lot_correction
            ON lot_correction.target_kind = 'lot'
           AND lot_correction.lot_id = l.id
          CROSS JOIN bounds
          WHERE %s >= 15
            AND l.geom && ST_Expand(
              ST_Transform(bounds.geom_3857, 4326), %s
            )
        ), features AS (
          SELECT
            l.id::text AS id,
            l.id::text AS record_id,
            l.block_id::text AS block_id,
            d.name AS district,
            d.district_code,
            b.block_code,
            l.lot_code,
            right(l.lot_code, 4) AS display_code,
            l.cup_code,
            l.cod_mza,
            l.property_code,
            l.locality_code,
            l.lot_type_code,
            l.project_status,
            l.levels,
            ST_Area(l.geom::geography)::double precision AS area_m2,
            ST_Perimeter(l.geom::geography)::double precision AS perimeter_m,
            l.block_match_method,
            l.source,
            ST_AsMVTGeom(l.geom_3857, bounds.geom_3857, 4096, 16, true) AS geom
          FROM corrected l
          LEFT JOIN public.gis_blocks b ON b.id = l.block_id
          JOIN public.gis_districts d ON d.id = l.district_id
          CROSS JOIN bounds
        )
        SELECT COALESCE(ST_AsMVT(features, 'lots', 4096, 'geom'), '\\x'::bytea) AS tile
        FROM features
        WHERE geom IS NOT NULL
        """,
        (z, x, y, z, MAX_CORRECTION_DEGREES),
    )
    return bytes(row["tile"]) if row and row.get("tile") is not None else b""


def simplify_tolerance_for(zoom: float) -> float:
    """Tolerancia de ST_SimplifyPreserveTopology en grados segun el zoom.

    A partir de z17 no se simplifica: el usuario esta inspeccionando el lote y
    cualquier desplazamiento de vertice seria visible.
    """
    if zoom >= 17:
        return 0.0
    if zoom >= 15:
        return 0.0000045  # ~0.5 m
    return 0.000009  # ~1 m


def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def to_feature(row: dict, property_keys: tuple[str, ...]) -> dict:
    return {
        "type": "Feature",
        "id": str(row["id"]),
        "geometry": row["geometry"] if isinstance(row["geometry"], dict) else json.loads(row["geometry"]),
        "properties": {key: row.get(key) for key in property_keys},
    }


async def fetch_polygon_layer(
    pool: AsyncConnectionPool, table: str, bbox: BBox, property_keys: tuple[str, ...]
) -> dict:
    rows, availability = await asyncio.gather(
        fetch_all(
            pool,
            f"""
            SELECT id, {', '.join(property_keys)}, ST_AsGeoJSON(geom)::json AS geometry
            FROM public.{table}
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
              AND ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            ORDER BY id
            """,
            [*bbox.as_params(), *bbox.as_params()],
        ),
        fetch_one(pool, f"SELECT EXISTS (SELECT 1 FROM public.{table}) AS available"),
    )
    features = [to_feature(row, property_keys) for row in rows]
    return {
        "data": feature_collection(features),
        "meta": {"available": bool(availability and availability["available"]), "total": len(features), "hasMore": False},
    }


async def fetch_zoom_limited_layer(
    pool: AsyncConnectionPool,
    table: str,
    bbox: BBox,
    min_zoom: float,
    zoom: float,
    select_sql: str,
    property_keys: tuple[str, ...],
    geom_column: str,
    id_column: str,
) -> dict:
    """Capa poligonal que solo se sirve a partir de cierto zoom.

    ``select_sql`` debe declarar sus marcadores en este orden: la tolerancia de
    simplificacion y despues los cuatro valores de la envolvente ensanchada que
    prefiltra sobre la geometria indexada. El filtro fino sobre la geometria
    corregida lo agrega esta funcion.
    """
    available_row = await fetch_one(
        pool, f"SELECT EXISTS (SELECT 1 FROM public.{table}) AS available"
    )
    available = bool(available_row and available_row["available"])

    if zoom < min_zoom:
        # El conteo completo solo se paga en esta rama, que se sirve una vez por
        # sesion de paneo; en la rama caliente basta con el EXISTS de arriba.
        total_row = await fetch_one(pool, f"SELECT count(*)::int AS total FROM public.{table}")
        return {
            "data": feature_collection([]),
            "meta": {
                "available": available,
                "total": int(total_row["total"] if total_row else 0),
                "hasMore": False,
                "minZoom": min_zoom,
                "zoomLimited": available,
            },
        }

    rows = await fetch_all(
        pool,
        f"""
        {select_sql}
        WHERE {geom_column} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
          AND ST_Intersects({geom_column}, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        ORDER BY {id_column}
        """,
        [
            simplify_tolerance_for(zoom),
            *bbox.as_params(),
            *bbox.as_params(),
            *bbox.as_params(),
        ],
    )
    return {
        "data": feature_collection([to_feature(row, property_keys) for row in rows]),
        "meta": {
            "available": available,
            "total": len(rows),
            "hasMore": False,
            "minZoom": min_zoom,
            "zoomLimited": False,
        },
    }


async def fetch_block_layer(pool: AsyncConnectionPool, bbox: BBox, zoom: float) -> dict:
    keys = (
        "record_id", "district", "district_code", "block_code", "property_code",
        "block_type_code", "area_m2", "perimeter_m", "lot_count", "source",
        "correction_lng", "correction_lat", "corrected_at",
    )
    return await fetch_zoom_limited_layer(
        pool,
        "gis_blocks",
        bbox,
        13,
        zoom,
        """
        SELECT x.id, x.id::text AS record_id, x.district, x.district_code, x.block_code,
               x.property_code, x.block_type_code, x.area_m2, x.perimeter_m,
               x.lot_count, x.source, x.correction_lng, x.correction_lat,
               x.corrected_at,
               ST_AsGeoJSON(
                 ST_SimplifyPreserveTopology(x.effective_geom, %s),
                 """ + str(GEOJSON_DECIMALS) + """
               )::json AS geometry
        FROM (
          SELECT b.id, d.name AS district, d.district_code, b.block_code,
                 b.property_code, b.block_type_code,
                 ST_Area(b.geom::geography)::float8 AS area_m2,
                 ST_Perimeter(b.geom::geography)::float8 AS perimeter_m,
                 (SELECT count(*)::int FROM public.gis_lots l WHERE l.block_id = b.id) AS lot_count,
                 b.source, COALESCE(c.delta_lng, 0)::float8 AS correction_lng,
                 COALESCE(c.delta_lat, 0)::float8 AS correction_lat,
                 c.updated_at AS corrected_at,
                 ST_Translate(b.geom, COALESCE(c.delta_lng, 0), COALESCE(c.delta_lat, 0)) AS effective_geom
          FROM public.gis_blocks b
          JOIN public.gis_districts d ON d.id = b.district_id
          LEFT JOIN public.gis_geometry_corrections c
            ON c.target_kind = 'block' AND c.block_id = b.id
          -- Prefiltro sobre la geometria original para que entre idx_gis_blocks_geom:
          -- el predicado final se aplica sobre effective_geom, que es una expresion
          -- y por tanto no puede usar el indice.
          WHERE b.geom && ST_Expand(
            ST_MakeEnvelope(%s, %s, %s, %s, 4326), """ + str(MAX_CORRECTION_DEGREES) + """
          )
        ) x
        """,
        keys,
        "x.effective_geom",
        "x.id",
    )


async def fetch_lot_layer(pool: AsyncConnectionPool, bbox: BBox, zoom: float) -> dict:
    keys = (
        "record_id", "district", "district_code", "block_code", "lot_code", "display_code", "cup_code",
        "cod_mza", "property_code", "locality_code", "lot_type_code",
        "project_status", "levels", "area_m2", "perimeter_m", "block_match_method", "source",
        "correction_lng", "correction_lat", "corrected_at",
    )
    return await fetch_zoom_limited_layer(
        pool,
        "gis_lots",
        bbox,
        15,
        zoom,
        """
        SELECT x.id, x.id::text AS record_id, x.district, x.district_code, x.block_code, x.lot_code,
               x.display_code, x.cup_code, x.cod_mza, x.property_code,
               x.locality_code, x.lot_type_code, x.project_status, x.levels,
               x.area_m2, x.perimeter_m, x.block_match_method, x.source,
               x.correction_lng, x.correction_lat, x.corrected_at,
               ST_AsGeoJSON(
                 ST_SimplifyPreserveTopology(x.effective_geom, %s),
                 """ + str(GEOJSON_DECIMALS) + """
               )::json AS geometry
        FROM (
          SELECT l.id, d.name AS district, d.district_code, b.block_code,
                 l.lot_code, right(l.lot_code, 4) AS display_code,
                 l.cup_code, l.cod_mza, l.property_code,
                 l.locality_code, l.lot_type_code, l.project_status, l.levels,
                 ST_Area(l.geom::geography)::float8 AS area_m2,
                 ST_Perimeter(l.geom::geography)::float8 AS perimeter_m,
                 l.block_match_method, l.source,
                 COALESCE(cl.delta_lng, 0)::float8 AS correction_lng,
                 COALESCE(cl.delta_lat, 0)::float8 AS correction_lat,
                 cl.updated_at AS corrected_at,
                 ST_Translate(
                   l.geom,
                   COALESCE(cb.delta_lng, 0) + COALESCE(cl.delta_lng, 0),
                   COALESCE(cb.delta_lat, 0) + COALESCE(cl.delta_lat, 0)
                 ) AS effective_geom
          FROM public.gis_lots l
          JOIN public.gis_districts d ON d.id = l.district_id
          LEFT JOIN public.gis_blocks b ON b.id = l.block_id
          LEFT JOIN public.gis_geometry_corrections cb
            ON cb.target_kind = 'block' AND cb.block_id = b.id
          LEFT JOIN public.gis_geometry_corrections cl
            ON cl.target_kind = 'lot' AND cl.lot_id = l.id
          -- Prefiltro sobre la geometria original para que entre idx_gis_lots_geom:
          -- el predicado final se aplica sobre effective_geom, que es una expresion
          -- y por tanto no puede usar el indice.
          WHERE l.geom && ST_Expand(
            ST_MakeEnvelope(%s, %s, %s, %s, 4326), """ + str(MAX_CORRECTION_DEGREES) + """
          )
        ) x
        """,
        keys,
        "x.effective_geom",
        "x.id",
    )


def district_simplify_tolerance_for(zoom: float) -> float:
    """Los limites distritales son multipoligonos con decenas de miles de vertices.

    A zoom bajo se muestran como fondo tematico, asi que se simplifican con fuerza;
    el detalle solo importa cuando se esta inspeccionando el borde.
    """
    if zoom >= 14:
        return 0.00002  # ~2 m
    if zoom >= 12:
        return 0.0001  # ~11 m
    return 0.0005  # ~55 m


async def fetch_district_layer(pool: AsyncConnectionPool, bbox: BBox, zoom: float = 10) -> dict:
    rows, availability = await asyncio.gather(
        fetch_all(
            pool,
            f"""
            SELECT d.id, d.name, d.district_code, d.province, d.department, d.source,
                   COALESCE(stats.supply_count, 0)::int AS supply_count,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(d.geom, %s),
                     {GEOJSON_DECIMALS}
                   )::json AS geometry
            FROM public.gis_districts d
            LEFT JOIN LATERAL (
              SELECT count(*) AS supply_count
              FROM public.gis_supply_locations sl
              WHERE sl.geom && d.geom
                AND ST_Covers(d.geom, sl.geom)
            ) stats ON true
            WHERE d.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
              AND ST_Intersects(d.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            ORDER BY d.district_code NULLS LAST, d.name
            """,
            [district_simplify_tolerance_for(zoom), *bbox.as_params(), *bbox.as_params()],
        ),
        fetch_one(pool, "SELECT EXISTS (SELECT 1 FROM public.gis_districts) AS available"),
    )
    keys = ("name", "district_code", "province", "department", "source", "supply_count")
    features = [to_feature(row, keys) for row in rows]
    return {
        "data": feature_collection(features),
        "meta": {
            "available": bool(availability and availability["available"]),
            "total": len(features),
            "hasMore": False,
        },
    }


async def fetch_district_catalog(pool: AsyncConnectionPool) -> list[dict]:
    """Catalogo para el combo de distritos.

    Devuelve la envolvente y un punto interior de cada distrito para que el
    cliente pueda encuadrar la camara sin tener que descargar la geometria.
    """
    return await fetch_all(
        pool,
        """
        SELECT d.district_code, d.name, COALESCE(stats.supply_count, 0)::int AS supply_count,
               ARRAY[
                 ST_XMin(env.box), ST_YMin(env.box),
                 ST_XMax(env.box), ST_YMax(env.box)
               ]::float8[] AS bounds,
               ARRAY[ST_X(surface.point), ST_Y(surface.point)]::float8[] AS center
        FROM public.gis_districts d
        CROSS JOIN LATERAL (SELECT d.geom::box2d::geometry AS box) env
        CROSS JOIN LATERAL (SELECT ST_PointOnSurface(d.geom) AS point) surface
        LEFT JOIN LATERAL (
          SELECT count(*) AS supply_count
          FROM public.gis_supply_locations sl
          WHERE sl.geom && d.geom
            AND ST_Covers(d.geom, sl.geom)
        ) stats ON true
        ORDER BY d.district_code NULLS LAST, d.name
        """,
    )


async def fetch_pipe_layer(pool: AsyncConnectionPool, bbox: BBox, network_type: str) -> dict:
    rows, availability = await asyncio.gather(
        fetch_all(
            pool,
            """
            SELECT id, network_type::text, network_level::text, material::text,
                   diameter_mm, condition::text,
                   COALESCE(length_m, ST_Length(geom::geography))::float8 AS length_m,
                   ST_AsGeoJSON(geom)::json AS geometry
            FROM public.network_pipes
            WHERE network_type::text = %s
              AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
              AND ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            ORDER BY id
            """,
            [network_type, *bbox.as_params(), *bbox.as_params()],
        ),
        fetch_one(
            pool,
            "SELECT EXISTS (SELECT 1 FROM public.network_pipes WHERE network_type::text = %s) AS available",
            [network_type],
        ),
    )
    keys = ("network_type", "network_level", "material", "diameter_mm", "condition", "length_m")
    features = [to_feature(row, keys) for row in rows]
    return {
        "data": feature_collection(features),
        "meta": {"available": bool(availability and availability["available"]), "total": len(features), "hasMore": False},
    }


# El diámetro real de un suministro casi nunca vive en cs.meter_diameter (está
# NULL en ~79% de las filas); cs.connection_diameter tiene el mismo dominio de
# valores y está poblado con mucha más frecuencia, así que sirve de respaldo.
# Ambos son texto libre de facturación ("0", "1", "160"...) que no siempre
# corresponde a un diámetro real de medidor, por eso se valida contra
# diameter_catalog en vez de mostrar el texto crudo.
RESOLVED_DIAMETER_SQL = """
    LEFT JOIN LATERAL (
      SELECT dc.diameter_mm
      FROM public.diameter_catalog dc
      WHERE dc.value_text = COALESCE(NULLIF(cs.meter_diameter, ''), NULLIF(cs.connection_diameter, ''))
    ) resolved_diameter ON true
"""

SUPPLY_BASE_SQL = f"""
    FROM public.customer_supplies cs
    JOIN public.gis_supply_locations sl ON sl.supply_id = cs.id
    LEFT JOIN LATERAL (
      SELECT mr.num_medidor, mr.anio_fabric::date AS installation_date, mr.registry_status
      FROM public.meter_registry mr
      WHERE mr.nis_rad = cs.supply_code
      ORDER BY (mr.registry_status = 'instalado') DESC, mr.anio_fabric DESC NULLS LAST, mr.num_medidor DESC
      LIMIT 1
    ) meter ON true
    {RESOLVED_DIAMETER_SQL}
    WHERE sl.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
      AND ST_Intersects(sl.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
"""


async def fetch_supply_layer(
    pool: AsyncConnectionPool,
    bbox: BBox,
    page: int,
    page_size: int,
    meters_only: bool,
    district: str | None,
) -> dict:
    offset = (page - 1) * page_size
    meter_filter = "AND COALESCE(meter.num_medidor, cs.meter_code) IS NOT NULL" if meters_only else ""
    district_filter = """
      AND (%s::text IS NULL OR EXISTS (
        SELECT 1
        FROM public.gis_districts filter_district
        WHERE filter_district.name = %s
          AND filter_district.geom && sl.geom
          AND ST_Covers(filter_district.geom, sl.geom)
      ))
    """
    params = [*bbox.as_params(), *bbox.as_params(), district, district]
    count_row, rows, availability = await asyncio.gather(
        fetch_one(
            pool,
            f"SELECT count(*)::int AS total {SUPPLY_BASE_SQL} {meter_filter} {district_filter}",
            params,
        ),
        fetch_all(
            pool,
            f"""
            SELECT cs.id, cs.supply_code, cs.customer_name, cs.service_address, cs.district,
                   cs.sector, cs.lot_code, cs.supply_status,
                   COALESCE(meter.num_medidor, cs.meter_code) AS meter_code,
                   resolved_diameter.diameter_mm::text AS meter_diameter,
                   meter.installation_date::text,
                   ST_AsGeoJSON(sl.geom)::json AS geometry
            {SUPPLY_BASE_SQL}
            {meter_filter}
            {district_filter}
            ORDER BY cs.supply_code
            LIMIT %s OFFSET %s
            """,
            [*params, page_size, offset],
        ),
        fetch_one(
            pool,
            "SELECT EXISTS (SELECT 1 FROM public.gis_supply_locations) AS available",
        ),
    )
    total = int(count_row["total"] if count_row else 0)
    keys = (
        "supply_code", "customer_name", "service_address", "district", "sector", "lot_code",
        "supply_status", "meter_code", "meter_diameter", "installation_date",
    )
    features = [to_feature(row, keys) for row in rows]
    return {
        "data": feature_collection(features),
        "meta": {
            "available": bool(availability and availability["available"]),
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": max(1, (total + page_size - 1) // page_size),
            "hasMore": offset + len(features) < total,
        },
    }


async def fetch_layers(
    pool: AsyncConnectionPool,
    bbox: BBox,
    layers: list[str],
    page: int,
    page_size: int,
    district: str | None = None,
    zoom: float = 10,
) -> dict:
    factories: dict[str, Callable[[], Awaitable[dict]]] = {
        "distritos": lambda: fetch_district_layer(pool, bbox, zoom),
        "manzanas": lambda: fetch_block_layer(pool, bbox, zoom),
        "cuadrantes": lambda: fetch_polygon_layer(pool, "gis_quadrants", bbox, ("code", "name", "source")),
        "lotes": lambda: fetch_lot_layer(pool, bbox, zoom),
        "tuberias": lambda: fetch_pipe_layer(pool, bbox, "agua_potable"),
        "alcantarillado": lambda: fetch_pipe_layer(pool, bbox, "alcantarillado"),
        "suministros": lambda: fetch_supply_layer(pool, bbox, page, page_size, False, district),
        "medidores": lambda: fetch_supply_layer(pool, bbox, page, page_size, True, district),
    }
    results = await asyncio.gather(*(factories[name]() for name in layers))
    return {"bbox": bbox.model_dump(), "layers": dict(zip(layers, results, strict=True))}


async def fetch_supply_detail(pool: AsyncConnectionPool, supply_code: str) -> dict | None:
    return await fetch_one(
        pool,
        f"""
        SELECT cs.id::text AS id, cs.supply_code, cs.customer_name, cs.service_address,
               cs.district AS district_text, cs.sector, cs.lot_code, cs.supply_status,
               cs.latitude, cs.longitude, cs.location_source, cs.location_quality,
               district.name AS district,
               structured_district.name AS structured_district_name,
               supply_link.district_code AS structured_district_code,
               supply_link.district_match_status,
               logical_lot.cod_mza AS structured_block_code,
               logical_lot.cup_code AS structured_cup_code,
               logical_lot.geometry_match_status,
               logical_lot.gis_lot_match_count,
               supply_link.cua_label,
               supply_link.cua_match_method,
               cua_catalog.code AS cua_code,
               cua_catalog.description AS cua_catalog_description,
               COALESCE(meter.num_medidor, cs.meter_code) AS meter_code,
               resolved_diameter.diameter_mm::text AS meter_diameter,
               meter.anio_fabric::date::text AS installation_date,
               meter.registry_status AS meter_status,
               ST_AsGeoJSON(sl.geom)::json AS geometry,
               COALESCE(structured_lot.record_id, spatial_lot.record_id) AS resolved_lot_id,
               COALESCE(structured_lot.lot_code, spatial_lot.lot_code) AS resolved_lot_code,
               COALESCE(structured_lot.block_code, spatial_lot.block_code) AS resolved_block_code,
               CASE WHEN structured_lot.record_id IS NOT NULL THEN 'CUPCODE' ELSE 'SPATIAL' END
                 AS resolved_lot_method
        FROM public.customer_supplies cs
        LEFT JOIN public.gis_supply_locations sl ON sl.supply_id = cs.id
        LEFT JOIN public.gis_supply_lot_links supply_link ON supply_link.supply_id = cs.id
        LEFT JOIN public.gis_cadastral_lot_units logical_lot
          ON logical_lot.cup_code = supply_link.cup_code
        LEFT JOIN public.territory_districts structured_district
          ON structured_district.district_code = supply_link.district_code
        LEFT JOIN public.supervision_code_catalog cua_catalog
          ON cua_catalog.id = supply_link.cua_catalog_id
        LEFT JOIN LATERAL (
          SELECT mr.num_medidor, mr.anio_fabric, mr.registry_status
          FROM public.meter_registry mr
          WHERE mr.nis_rad = cs.supply_code
          ORDER BY (mr.registry_status = 'instalado') DESC, mr.anio_fabric DESC NULLS LAST, mr.num_medidor DESC
          LIMIT 1
        ) meter ON true
        {RESOLVED_DIAMETER_SQL}
        LEFT JOIN LATERAL (
          SELECT d.name
          FROM public.gis_districts d
          WHERE sl.geom IS NOT NULL AND ST_Covers(d.geom, sl.geom)
          LIMIT 1
        ) district ON true
        -- Resuelve el lote catastral real que contiene el punto del suministro.
        -- cs.lot_code es una referencia textual de facturación, no un id de
        -- gis_lots, así que no alcanza para enlazar ni encuadrar el lote en el mapa.
        --
        -- structured_lot y spatial_lot calculan la geometría corregida (ST_Translate)
        -- solo para el puñado de lotes candidatos (por cup_code, o los ~cientos que
        -- tienen una corrección activa en gis_geometry_corrections) en vez de para
        -- los ~2M de lotes de la ciudad: eso evitaba usar el índice GiST de gis_lots.geom
        -- y materializaba la tabla completa en cada consulta (~6s por suministro).
        LEFT JOIN LATERAL (
          SELECT lg.id::text AS record_id, lg.lot_code, blk.block_code
          FROM public.gis_cadastral_lot_geometries bridge
          JOIN public.gis_lots lg ON lg.id = bridge.gis_lot_id
          LEFT JOIN public.gis_blocks blk ON blk.id = lg.block_id
          LEFT JOIN public.gis_geometry_corrections cb ON cb.target_kind = 'block' AND cb.block_id = blk.id
          LEFT JOIN public.gis_geometry_corrections cl ON cl.target_kind = 'lot' AND cl.lot_id = lg.id
          WHERE bridge.cup_code = supply_link.cup_code
            AND (
              logical_lot.gis_lot_match_count = 1
              OR (
                sl.geom IS NOT NULL
                AND ST_Covers(
                  ST_Translate(lg.geom, COALESCE(cb.delta_lng, 0) + COALESCE(cl.delta_lng, 0), COALESCE(cb.delta_lat, 0) + COALESCE(cl.delta_lat, 0)),
                  sl.geom
                )
              )
            )
          ORDER BY
            CASE WHEN sl.geom IS NOT NULL AND ST_Covers(
              ST_Translate(lg.geom, COALESCE(cb.delta_lng, 0) + COALESCE(cl.delta_lng, 0), COALESCE(cb.delta_lat, 0) + COALESCE(cl.delta_lat, 0)),
              sl.geom
            ) THEN 0 ELSE 1 END,
            ST_Area(lg.geom::geography)
          LIMIT 1
        ) structured_lot ON true
        LEFT JOIN LATERAL (
          SELECT candidate.record_id, candidate.lot_code, candidate.block_code
          FROM (
            -- Lotes sin corrección activa: geometría cruda, usa el índice GiST.
            SELECT lg.id::text AS record_id, lg.lot_code AS lot_code, blk.block_code AS block_code, lg.geom AS raw_geom
            FROM public.gis_lots lg
            LEFT JOIN public.gis_blocks blk ON blk.id = lg.block_id
            WHERE structured_lot.record_id IS NULL
              AND sl.geom IS NOT NULL
              AND ST_Covers(lg.geom, sl.geom)
              AND NOT EXISTS (
                SELECT 1 FROM public.gis_geometry_corrections cc
                WHERE (cc.target_kind = 'lot' AND cc.lot_id = lg.id)
                   OR (cc.target_kind = 'block' AND cc.block_id = lg.block_id)
              )

            UNION ALL

            -- Lotes dentro de una manzana con corrección (conjunto minúsculo en toda la ciudad).
            SELECT lg.id::text AS record_id, lg.lot_code AS lot_code, blk.block_code AS block_code, lg.geom AS raw_geom
            FROM public.gis_geometry_corrections cb
            JOIN public.gis_blocks blk ON blk.id = cb.block_id AND cb.target_kind = 'block'
            JOIN public.gis_lots lg ON lg.block_id = blk.id
            LEFT JOIN public.gis_geometry_corrections cl ON cl.target_kind = 'lot' AND cl.lot_id = lg.id
            WHERE structured_lot.record_id IS NULL
              AND sl.geom IS NOT NULL
              AND ST_Covers(
                ST_Translate(lg.geom, cb.delta_lng + COALESCE(cl.delta_lng, 0), cb.delta_lat + COALESCE(cl.delta_lat, 0)),
                sl.geom
              )

            UNION ALL

            -- Lotes con corrección propia (independiente de su manzana).
            SELECT lg.id::text AS record_id, lg.lot_code AS lot_code, blk.block_code AS block_code, lg.geom AS raw_geom
            FROM public.gis_geometry_corrections cl
            JOIN public.gis_lots lg ON lg.id = cl.lot_id AND cl.target_kind = 'lot'
            LEFT JOIN public.gis_blocks blk ON blk.id = lg.block_id
            LEFT JOIN public.gis_geometry_corrections cb ON cb.target_kind = 'block' AND cb.block_id = blk.id
            WHERE structured_lot.record_id IS NULL
              AND sl.geom IS NOT NULL
              AND ST_Covers(
                ST_Translate(lg.geom, COALESCE(cb.delta_lng, 0) + cl.delta_lng, COALESCE(cb.delta_lat, 0) + cl.delta_lat),
                sl.geom
              )
          ) candidate
          ORDER BY ST_Area(candidate.raw_geom::geography)
          LIMIT 1
        ) spatial_lot ON true
        WHERE cs.supply_code = %s
        LIMIT 1
        """,
        [supply_code],
    )


async def fetch_supply_consumption(pool: AsyncConnectionPool, supply_code: str) -> dict | None:
    """Obtiene el cálculo costoso después de haber mostrado la ficha base."""
    return await fetch_one(
        pool,
        """
        WITH selected_supply AS (
          SELECT supply_code, district
          FROM public.customer_supplies
          WHERE supply_code = %s
          LIMIT 1
        ), supply_consumption AS (
          SELECT avg(b.billed_volume_m3)::float8 AS supply_avg_m3,
                 count(*)::int AS supply_reading_count
          FROM public.customer_supply_billing_daily b
          JOIN selected_supply cs ON cs.supply_code = b.supply_code
          WHERE b.billed_volume_m3 IS NOT NULL
            AND COALESCE(b.reading_date, b.issue_date) >= date_trunc('year', now())
            AND COALESCE(b.reading_date, b.issue_date) < date_trunc('year', now()) + interval '1 year'
        ), district_consumption AS (
          SELECT avg(b.billed_volume_m3)::float8 AS district_avg_m3,
                 count(DISTINCT b.supply_code)::int AS district_supply_count
          FROM public.customer_supply_billing_daily b
          JOIN public.customer_supplies peer ON peer.supply_code = b.supply_code
          JOIN selected_supply cs ON cs.district = peer.district
          WHERE b.billed_volume_m3 IS NOT NULL
            AND COALESCE(b.reading_date, b.issue_date) >= date_trunc('year', now())
            AND COALESCE(b.reading_date, b.issue_date) < date_trunc('year', now()) + interval '1 year'
        )
        SELECT supply_avg_m3, supply_reading_count, district_avg_m3, district_supply_count
        FROM supply_consumption CROSS JOIN district_consumption
        """,
        [supply_code],
    )


async def search_cadastre(
    pool: AsyncConnectionPool, query: str, kind: str, limit: int
) -> list[dict]:
    normalized = query.strip()
    prefix = f"{normalized}%"
    contains = f"%{normalized}%"
    rows = await fetch_all(
        pool,
        """
        WITH block_geometries AS (
          SELECT b.*, COALESCE(c.delta_lng, 0)::float8 AS correction_lng,
                 COALESCE(c.delta_lat, 0)::float8 AS correction_lat,
                 c.updated_at AS corrected_at,
                 ST_Translate(b.geom, COALESCE(c.delta_lng, 0), COALESCE(c.delta_lat, 0)) AS effective_geom
          FROM public.gis_blocks b
          LEFT JOIN public.gis_geometry_corrections c
            ON c.target_kind = 'block' AND c.block_id = b.id
        ), lot_geometries AS (
          SELECT l.*, COALESCE(cl.delta_lng, 0)::float8 AS correction_lng,
                 COALESCE(cl.delta_lat, 0)::float8 AS correction_lat,
                 cl.updated_at AS corrected_at,
                 ST_Translate(
                   l.geom,
                   COALESCE(cb.delta_lng, 0) + COALESCE(cl.delta_lng, 0),
                   COALESCE(cb.delta_lat, 0) + COALESCE(cl.delta_lat, 0)
                 ) AS effective_geom
          FROM public.gis_lots l
          LEFT JOIN public.gis_blocks b ON b.id = l.block_id
          LEFT JOIN public.gis_geometry_corrections cb
            ON cb.target_kind = 'block' AND cb.block_id = b.id
          LEFT JOIN public.gis_geometry_corrections cl
            ON cl.target_kind = 'lot' AND cl.lot_id = l.id
        ), results AS (
          SELECT 'block'::text AS kind, b.id::text, b.block_code AS code,
                 CASE WHEN lower(b.block_code) = lower(%s) THEN 0 ELSE 1 END AS rank,
                 jsonb_build_array(
                   ST_X(ST_PointOnSurface(b.effective_geom)), ST_Y(ST_PointOnSurface(b.effective_geom))
                 ) AS center,
                 jsonb_build_object(
                   'district', d.name, 'district_code', d.district_code,
                   'block_code', b.block_code, 'property_code', b.property_code,
                   'block_type_code', b.block_type_code,
                   'area_m2', ST_Area(b.geom::geography),
                   'perimeter_m', ST_Perimeter(b.geom::geography),
                   'lot_count', (SELECT count(*) FROM public.gis_lots l WHERE l.block_id = b.id),
                   'correction_lng', b.correction_lng, 'correction_lat', b.correction_lat,
                   'corrected_at', b.corrected_at,
                   'source', b.source
                 ) AS properties
          FROM block_geometries b
          JOIN public.gis_districts d ON d.id = b.district_id
          WHERE %s IN ('all', 'block') AND b.block_code ILIKE %s

          UNION ALL

          SELECT 'lot'::text AS kind, l.id::text, l.lot_code AS code,
                 CASE WHEN lower(l.lot_code) = lower(%s)
                            OR lower(COALESCE(l.cup_code, '')) = lower(%s)
                            OR lower(COALESCE(l.property_code, '')) = lower(%s)
                      THEN 0 ELSE 1 END AS rank,
                 jsonb_build_array(
                   ST_X(ST_PointOnSurface(l.effective_geom)), ST_Y(ST_PointOnSurface(l.effective_geom))
                 ) AS center,
                 jsonb_build_object(
                   'district', d.name, 'district_code', d.district_code,
                   'block_code', b.block_code, 'lot_code', l.lot_code,
                   'display_code', right(l.lot_code, 4),
                   'cup_code', l.cup_code, 'cod_mza', l.cod_mza,
                   'property_code', l.property_code, 'locality_code', l.locality_code,
                   'lot_type_code', l.lot_type_code, 'project_status', l.project_status,
                   'levels', l.levels, 'area_m2', ST_Area(l.geom::geography),
                   'perimeter_m', ST_Perimeter(l.geom::geography),
                   'correction_lng', l.correction_lng, 'correction_lat', l.correction_lat,
                   'corrected_at', l.corrected_at,
                   'block_match_method', l.block_match_method, 'source', l.source
                 ) AS properties
          FROM lot_geometries l
          JOIN public.gis_districts d ON d.id = l.district_id
          LEFT JOIN public.gis_blocks b ON b.id = l.block_id
          WHERE %s IN ('all', 'lot')
            AND (l.lot_code ILIKE %s OR l.cup_code ILIKE %s OR l.property_code ILIKE %s)
        )
        SELECT kind, id, code, center, properties
        FROM results
        ORDER BY rank, kind, code
        LIMIT %s
        """,
        [
            normalized,
            kind,
            prefix,
            normalized,
            normalized,
            normalized,
            kind,
            prefix,
            contains,
            contains,
            limit,
        ],
    )
    return rows


async def resolve_location(pool: AsyncConnectionPool, lng: float, lat: float, tolerance_m: float) -> dict:
    row = await fetch_one(
        pool,
        """
        WITH point AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS geom),
        block_geometries AS (
          SELECT b.*,
                 ST_Translate(b.geom, COALESCE(c.delta_lng, 0), COALESCE(c.delta_lat, 0)) AS effective_geom
          FROM public.gis_blocks b
          LEFT JOIN public.gis_geometry_corrections c
            ON c.target_kind = 'block' AND c.block_id = b.id
        ), lot_geometries AS (
          SELECT l.*,
                 ST_Translate(
                   l.geom,
                   COALESCE(cb.delta_lng, 0) + COALESCE(cl.delta_lng, 0),
                   COALESCE(cb.delta_lat, 0) + COALESCE(cl.delta_lat, 0)
                 ) AS effective_geom
          FROM public.gis_lots l
          LEFT JOIN public.gis_blocks b ON b.id = l.block_id
          LEFT JOIN public.gis_geometry_corrections cb
            ON cb.target_kind = 'block' AND cb.block_id = b.id
          LEFT JOIN public.gis_geometry_corrections cl
            ON cl.target_kind = 'lot' AND cl.lot_id = l.id
        )
        SELECT
          (SELECT jsonb_build_object('id', d.id, 'name', d.name)
             FROM public.gis_districts d, point p WHERE ST_Covers(d.geom, p.geom) LIMIT 1) AS district,
          (SELECT jsonb_build_object('id', q.id, 'code', q.code, 'name', q.name)
             FROM public.gis_quadrants q, point p WHERE ST_Covers(q.geom, p.geom) LIMIT 1) AS quadrant,
          (SELECT jsonb_build_object(
              'id', b.id, 'blockCode', b.block_code, 'propertyCode', b.property_code,
              'blockTypeCode', b.block_type_code,
              'areaM2', ST_Area(b.geom::geography),
              'perimeterM', ST_Perimeter(b.geom::geography))
             FROM block_geometries b, point p
             WHERE ST_Covers(b.effective_geom, p.geom)
             ORDER BY ST_Area(b.geom::geography) LIMIT 1) AS block,
          (SELECT jsonb_build_object(
              'id', l.id, 'lotCode', l.lot_code, 'cupCode', l.cup_code,
              'propertyCode', l.property_code, 'lotTypeCode', l.lot_type_code,
              'projectStatus', l.project_status, 'levels', l.levels,
              'blockCode', b.block_code, 'areaM2', ST_Area(l.geom::geography),
              'perimeterM', ST_Perimeter(l.geom::geography))
             FROM lot_geometries l
             LEFT JOIN public.gis_blocks b ON b.id = l.block_id
             CROSS JOIN point p
             WHERE ST_Covers(l.effective_geom, p.geom)
             ORDER BY ST_Area(l.geom::geography) LIMIT 1) AS lot,
          (SELECT jsonb_build_object(
              'id', cs.id, 'supplyCode', cs.supply_code, 'sector', cs.sector,
              'lotCode', cs.lot_code, 'distanceMeters', ST_Distance(sl.geom::geography, p.geom::geography)
            )
             FROM public.customer_supplies cs
             JOIN public.gis_supply_locations sl ON sl.supply_id = cs.id
             CROSS JOIN point p
             WHERE ST_DWithin(sl.geom::geography, p.geom::geography, %s)
             ORDER BY sl.geom <-> p.geom LIMIT 1) AS supply
        """,
        [lng, lat, tolerance_m],
    )
    return row or {"district": None, "quadrant": None, "block": None, "lot": None, "supply": None}


async def save_geometry_correction(
    pool: AsyncConnectionPool,
    target_kind: str,
    target_id: str,
    delta_lng: float,
    delta_lat: float,
    updated_by: str | None,
    reset: bool,
) -> dict | None:
    target_table = "gis_blocks" if target_kind == "block" else "gis_lots"
    target_column = "block_id" if target_kind == "block" else "lot_id"
    other_column = "lot_id" if target_kind == "block" else "block_id"

    async with pool.connection() as connection:
        async with connection.transaction():
            target = await connection.execute(
                f"SELECT id FROM public.{target_table} WHERE id = %s::uuid",
                [target_id],
            )
            if await target.fetchone() is None:
                return None
            if reset:
                await connection.execute(
                    f"DELETE FROM public.gis_geometry_corrections WHERE target_kind = %s AND {target_column} = %s::uuid",
                    [target_kind, target_id],
                )
                return {
                    "targetKind": target_kind,
                    "targetId": target_id,
                    "deltaLng": 0,
                    "deltaLat": 0,
                    "reset": True,
                }

            async def is_valid(candidate_lng: float, candidate_lat: float) -> bool:
                if target_kind == "lot":
                    validation = await connection.execute(
                        """
                        SELECT
                          ST_Distance(
                            ST_PointOnSurface(l.geom)::geography,
                            ST_PointOnSurface(ST_Translate(l.geom, %s, %s))::geography
                          ) <= %s
                          AND ST_CoveredBy(
                            ST_Translate(
                              l.geom,
                              COALESCE(block_correction.delta_lng, 0) + %s,
                              COALESCE(block_correction.delta_lat, 0) + %s
                            ),
                            ST_Translate(
                              b.geom,
                              COALESCE(block_correction.delta_lng, 0),
                              COALESCE(block_correction.delta_lat, 0)
                            )
                          ) AS valid
                        FROM public.gis_lots l
                        JOIN public.gis_blocks b ON b.id = l.block_id
                        LEFT JOIN public.gis_geometry_corrections block_correction
                          ON block_correction.target_kind = 'block'
                         AND block_correction.block_id = b.id
                        WHERE l.id = %s::uuid
                        """,
                        [candidate_lng, candidate_lat, MAX_MANUAL_CORRECTION_METERS, candidate_lng, candidate_lat, target_id],
                    )
                else:
                    validation = await connection.execute(
                        """
                        WITH candidate AS (
                          SELECT b.id, b.district_id,
                                 ST_Translate(b.geom, %s, %s) AS geom,
                                 b.geom AS official_geom
                          FROM public.gis_blocks b
                          WHERE b.id = %s::uuid
                        )
                        SELECT
                          ST_Distance(
                            ST_PointOnSurface(candidate.official_geom)::geography,
                            ST_PointOnSurface(candidate.geom)::geography
                          ) <= %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM public.gis_blocks other
                            LEFT JOIN public.gis_geometry_corrections other_correction
                              ON other_correction.target_kind = 'block'
                             AND other_correction.block_id = other.id
                            WHERE other.id <> candidate.id
                              AND other.district_id = candidate.district_id
                              AND ST_Intersects(
                                candidate.geom,
                                ST_Translate(
                                  other.geom,
                                  COALESCE(other_correction.delta_lng, 0),
                                  COALESCE(other_correction.delta_lat, 0)
                                )
                              )
                              AND ST_Area(ST_Intersection(
                                candidate.geom,
                                ST_Translate(
                                  other.geom,
                                  COALESCE(other_correction.delta_lng, 0),
                                  COALESCE(other_correction.delta_lat, 0)
                                )
                              )::geography) > 0.25
                          ) AS valid
                        FROM candidate
                        """,
                        [candidate_lng, candidate_lat, target_id, MAX_MANUAL_CORRECTION_METERS],
                    )
                row = await validation.fetchone()
                return bool(row and row[0])

            applied_lng = delta_lng
            applied_lat = delta_lat
            limited = False
            if not await is_valid(applied_lng, applied_lat):
                limited = True
                low = 0.0
                high = 1.0
                for _ in range(16):
                    factor = (low + high) / 2
                    if await is_valid(delta_lng * factor, delta_lat * factor):
                        low = factor
                    else:
                        high = factor
                applied_lng = delta_lng * low
                applied_lat = delta_lat * low

            result = await connection.execute(
                f"""
                INSERT INTO public.gis_geometry_corrections
                  (target_kind, {target_column}, {other_column}, delta_lng, delta_lat, updated_by)
                VALUES (%s, %s::uuid, NULL, %s, %s, %s)
                ON CONFLICT ({target_column}) WHERE target_kind = '{target_kind}'
                DO UPDATE SET
                  delta_lng = EXCLUDED.delta_lng,
                  delta_lat = EXCLUDED.delta_lat,
                  updated_by = EXCLUDED.updated_by,
                  updated_at = now()
                RETURNING delta_lng, delta_lat, updated_at
                """,
                [target_kind, target_id, applied_lng, applied_lat, updated_by],
            )
            row = await result.fetchone()
            applied_meters = abs(applied_lng) * 111_320 + abs(applied_lat) * 111_320
            if limited and applied_meters < 0.02:
                limit_reason = (
                    "Este lote ya toca el borde de su manzana en esa dirección. Mueve la manzana completa o prueba otra dirección."
                    if target_kind == "lot"
                    else "La manzana no tiene espacio seguro en esa dirección porque alcanzaría otra manzana."
                )
            elif limited:
                limit_reason = "Se aplicó automáticamente el máximo desplazamiento seguro para no invadir pistas ni otras manzanas."
            else:
                limit_reason = None
            return {
                "targetKind": target_kind,
                "targetId": target_id,
                "deltaLng": row[0],
                "deltaLat": row[1],
                "updatedAt": row[2],
                "limited": limited,
                "limitReason": limit_reason,
                "reset": False,
            }


async def sync_supply_locations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            """
            DELETE FROM public.gis_supply_locations sl
            WHERE NOT EXISTS (
              SELECT 1
              FROM public.customer_supplies cs
              WHERE cs.id = sl.supply_id
                AND cs.longitude BETWEEN -180 AND 180
                AND cs.latitude BETWEEN -90 AND 90
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO public.gis_supply_locations
              (supply_id, supply_code, geom, source_updated_at, synced_at)
            SELECT id, supply_code, ST_SetSRID(ST_MakePoint(longitude, latitude), 4326), updated_at, now()
            FROM public.customer_supplies
            WHERE longitude BETWEEN -180 AND 180 AND latitude BETWEEN -90 AND 90
            ON CONFLICT (supply_id) DO UPDATE SET
              supply_code = EXCLUDED.supply_code,
              geom = EXCLUDED.geom,
              source_updated_at = EXCLUDED.source_updated_at,
              synced_at = now()
            WHERE public.gis_supply_locations.source_updated_at IS DISTINCT FROM EXCLUDED.source_updated_at
               OR NOT ST_Equals(public.gis_supply_locations.geom, EXCLUDED.geom)
            """
        )


async def fetch_lot_context(pool: AsyncConnectionPool, lot_id: str) -> dict | None:
    # 1. Fetch Lot Summary
    lot_row = await fetch_one(
        pool,
        "SELECT id, lot_code, block_id FROM gis.lots WHERE id = %s::uuid",
        [lot_id],
    )
    if not lot_row:
        return None

    lot = {
        "id": str(lot_row["id"]),
        "lotCode": lot_row["lot_code"],
        "blockId": str(lot_row["block_id"]),
    }

    # 2. Fetch Current Holders
    holders_rows = await fetch_all(
        pool,
        """
        SELECT le.id AS legal_entity_id,
               le.legal_name,
               le.entity_type,
               rel.relationship_type,
               rel.valid_from,
               rel.valid_to
        FROM gis.lot_legal_entities rel
        JOIN gis.legal_entities le ON le.id = rel.legal_entity_id
        WHERE rel.lot_id = %s::uuid
          AND rel.valid_from <= CURRENT_DATE
          AND (rel.valid_to IS NULL OR rel.valid_to >= CURRENT_DATE)
          AND le.is_active = true
        ORDER BY rel.relationship_type, rel.valid_from DESC, le.legal_name
        """,
        [lot_id],
    )

    current_holders = []
    for r in holders_rows:
        current_holders.append({
            "legalEntityId": str(r["legal_entity_id"]),
            "legalName": r["legal_name"],
            "entityType": r["entity_type"],
            "relationshipType": r["relationship_type"],
            "validFrom": r["valid_from"].isoformat() if hasattr(r["valid_from"], "isoformat") else str(r["valid_from"]),
            "validTo": r["valid_to"].isoformat() if r["valid_to"] and hasattr(r["valid_to"], "isoformat") else (str(r["valid_to"]) if r["valid_to"] else None),
        })

    # 3. Fetch Supplies and their Connections / Meters
    supply_rows = await fetch_all(
        pool,
        """
        SELECT s.id AS supply_id,
               s.supply_code,
               s.service_status,
               c.id AS connection_id,
               c.asset_code AS connection_code,
               c.status AS connection_status,
               m.id AS meter_id,
               m.serial_number AS meter_serial,
               m.status AS meter_status
        FROM utility.supplies s
        LEFT JOIN utility.service_connections c ON c.id = s.connection_id
        LEFT JOIN utility.meters m ON m.supply_id = s.id
        WHERE s.lot_id = %s::uuid
        ORDER BY s.supply_code, m.installation_date DESC NULLS LAST, m.serial_number
        """,
        [lot_id],
    )

    supplies_map = {}
    for r in supply_rows:
        sid = r["supply_id"]
        if sid not in supplies_map:
            connection = None
            if r["connection_id"]:
                connection = {
                    "id": str(r["connection_id"]),
                    "assetCode": r["connection_code"] or "",
                    "status": r["connection_status"] or "",
                }
            supplies_map[sid] = {
                "id": str(sid),
                "supplyCode": r["supply_code"],
                "serviceStatus": r["service_status"],
                "connection": connection,
                "meters": [],
            }

        if r["meter_id"] and r["meter_serial"] and r["meter_status"]:
            supplies_map[sid]["meters"].append({
                "id": str(r["meter_id"]),
                "serialNumber": r["meter_serial"],
                "status": r["meter_status"],
            })

    supplies = list(supplies_map.values())

    return {
        "lot": lot,
        "currentHolders": current_holders,
        "supplies": supplies,
    }
