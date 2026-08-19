from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException
from datetime import datetime

from app.authz import is_admin, normalize_role
from app.database import get_pool
from app.repositories.diameters import list_diameter_catalog
from app.repositories.planillas import get_planilla_record_access
from app.repositories.supervision_mapping import VISIT_DATE_SQL_NO_ALIAS


def normalize_db_column_name(raw_name: str) -> str:
    import re

    return (
        re.sub(r"[^a-z0-9_]+", "_", raw_name.strip().lower()).strip("_")[:63]
    )


def format_visit_date_for_storage(date: str) -> str:
    year, month, day = date.split("-")
    return f"{day}/{month}/{year}"


def format_visit_date_for_input(date: str | None) -> str:
    import re

    if not date:
        return ""
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", date)
    if not match:
        return ""
    day, month, year = match.group(1), match.group(2), match.group(3)
    return f"{year}-{month}-{day}"


def to_input_date_if_possible(value: str | None) -> str | None:
    formatted = format_visit_date_for_input(value)
    return formatted or None


def normalize_cell_value(value) -> str | None:
    if value is None:
        return None
    trimmed = str(value).strip()
    return trimmed or None


def normalize_coordinate(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_column_comment(comment: str | None) -> dict:
    parts = [part.strip() for part in str(comment or "").split("|") if part.strip()]
    base_comment = None
    label = "Campo"
    order_os = None
    responsible = "SIN_DEFINIR"
    source_column = None

    for part in parts:
        if part.startswith("Orden OS:"):
            try:
                order_os = int(part.replace("Orden OS:", "").strip())
            except ValueError:
                order_os = None
            continue
        if part.startswith("Origen Excel:"):
            value = part.replace("Origen Excel:", "").strip()
            source_column = value or None
            continue
        if part.startswith("Responsable:"):
            value = part.replace("Responsable:", "").strip().upper()
            if value in {"SEDAPAL", "CONTRATISTA"}:
                responsible = value
            continue
        base_comment = part
        label = part

    return {
        "baseComment": base_comment,
        "label": label,
        "orderOs": order_os,
        "responsible": responsible,
        "sourceColumn": source_column,
    }


def resolve_field_group(order_os: int | None) -> str:
    if order_os is None:
        return "otros"
    if order_os < 100:
        return "general"
    if order_os < 150:
        return "inspeccion_interior"
    if order_os < 184:
        return "agua_medidor"
    return "alcantarillado"


def resolve_input_kind(label: str, catalog_options: list[dict], comment: str | None) -> str:
    normalized = f"{label} {comment or ''}".lower()
    if catalog_options:
        return "select"
    if "(s o n)" in normalized or ":s/n" in normalized or " s o n" in normalized or "si o no" in normalized:
        return "yes_no"
    if "observ" in normalized or "describe" in normalized or "referencia" in normalized:
        return "textarea"
    return "text"


# Columnas de diametro conocidas cuyo nombre no contiene literalmente "diametro"
# (p.ej. dia_conex_agua, dia_conex_des). Se listan de forma explicita para no
# depender por completo de que el COMMENT de la columna en Postgres mencione
# "diametro"/"diameter" -- si ese comentario falta o cambia, estos campos deben
# seguir mostrandose como selector, nunca como texto libre.
KNOWN_DIAMETER_COLUMNS = {
    "dia_med", "dia_med2", "dia_med3", "dia_conex_agua", "dia_conex_des",
}


def is_diameter_field(column_name: str, label: str, comment: str | None) -> bool:
    if column_name in KNOWN_DIAMETER_COLUMNS:
        return True
    normalized = f"{column_name} {label} {comment or ''}".lower()
    return "diametro" in normalized or "diameter" in normalized


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def get_field_rows(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT
                  c.column_name,
                  pg_catalog.col_description(pc.oid, c.ordinal_position::int) AS comment
                FROM information_schema.columns c
                JOIN pg_catalog.pg_class pc ON pc.relname = c.table_name
                JOIN pg_catalog.pg_namespace pn
                  ON pn.oid = pc.relnamespace
                 AND pn.nspname = c.table_schema
                WHERE c.table_schema = 'public'
                  AND c.table_name = 'supervision'
                  AND c.column_name NOT IN (
                    'supervision_id', 'created_at', 'updated_at', 'source_file_name', 'source_imported_at',
                    'assigned_user_id'
                  )
                ORDER BY c.ordinal_position;
                """
            )
            return await cursor.fetchall()


async def enrich_field_rows_with_local_metadata(
    pool: AsyncConnectionPool,
    field_rows: list[dict],
) -> list[dict]:
    commented_rows = sum(1 for row in field_rows if row.get("comment"))
    if commented_rows > 10:
        return field_rows

    try:
        local_pool = get_pool()
    except RuntimeError:
        return field_rows

    if local_pool is pool:
        return field_rows

    local_field_rows = await get_field_rows(local_pool)
    local_comment_map = {
        str(row["column_name"]): row.get("comment")
        for row in local_field_rows
        if row.get("comment")
    }

    enriched_rows: list[dict] = []
    for row in field_rows:
        comment = row.get("comment") or local_comment_map.get(str(row["column_name"]))
        enriched_rows.append(
            {
                **row,
                "comment": comment,
            }
        )

    return enriched_rows


async def get_catalog_map(pool: AsyncConnectionPool) -> dict[int, list[dict]]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT catalog_number, code, description, extra_1, extra_2
                FROM public.supervision_code_catalog
                ORDER BY catalog_number, display_order, code;
                """
            )
            rows = await cursor.fetchall()

    catalog_map: dict[int, list[dict]] = {}
    for row in rows:
        catalog_map.setdefault(int(row["catalog_number"]), []).append(
            {
                "code": row["code"],
                "description": row["description"],
                "extra1": row["extra_1"],
                "extra2": row["extra_2"],
            }
        )
    return catalog_map


async def build_field_meta(pool: AsyncConnectionPool) -> list[dict]:
    field_rows = await enrich_field_rows_with_local_metadata(pool, await get_field_rows(pool))
    local_pool = get_pool()
    catalog_map = await get_catalog_map(local_pool)
    response: list[dict] = []
    # "code" es lo que efectivamente se guarda en `supervision` al elegir una opcion
    # (ver FieldControl/SearchableSelect en el front). dia_conex_agua es numeric y
    # dia_med/dia_med2/dia_med3/dia_conex_des son varchar(5): un value_text con
    # formato libre ("15 mm") rompe el guardado (numeric) o se trunca (varchar(5)).
    # El mm puro es siempre seguro para ambos tipos; value_text queda solo como texto
    # visible en el selector.
    diameter_options = [
        {
            "code": str(option["diameterMm"]),
            "description": option["value"],
            "extra1": str(option["diameterMm"]),
            "extra2": None,
        }
        for option in await list_diameter_catalog(local_pool)
    ]
    
    personal_options = []
    try:
        async with local_pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SELECT ficha, nombre, rol FROM public.personal ORDER BY ficha;")
                personal_rows = await cursor.fetchall()
                for r in personal_rows:
                    personal_options.append({
                        "code": r["ficha"],
                        "description": f"{r['nombre']} ({r['rol']})" if r["rol"] else r["nombre"],
                        "extra1": None,
                        "extra2": None
                    })
    except Exception as e:
        print(f"Error fetching personal options: {e}")

    for row in field_rows:
        parsed = parse_column_comment(row["comment"])
        if row["column_name"] == "ficha":
            catalog_options = personal_options
        elif is_diameter_field(row["column_name"], parsed["label"], parsed["baseComment"]):
            catalog_options = diameter_options
        else:
            order_os = parsed["orderOs"] or -1
            if row["column_name"] in {"dia_med2", "dia_med3"}:
                order_os = 162
            catalog_options = catalog_map.get(order_os, [])
        response.append(
            {
                "catalogNumber": parsed["orderOs"],
                "catalogOptions": catalog_options,
                "columnName": row["column_name"],
                "comment": parsed["baseComment"],
                "editable": parsed["responsible"] == "CONTRATISTA",
                "group": resolve_field_group(parsed["orderOs"]),
                "inputKind": resolve_input_kind(parsed["label"], catalog_options, parsed["baseComment"]),
                "label": parsed["label"],
                "orderOs": parsed["orderOs"],
                "responsible": parsed["responsible"],
                "sourceColumn": parsed["sourceColumn"],
            }
        )
    return response


def build_import_header_alias_map(fields: list[dict]) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for field in fields:
        column_name = str(field.get("columnName") or "").strip()
        if not column_name:
            continue

        alias_map[normalize_db_column_name(column_name)] = column_name

        source_column = str(field.get("sourceColumn") or "").strip()
        if source_column:
            alias_map[normalize_db_column_name(source_column)] = column_name

    return alias_map


def build_import_row_objects(
    file_content: str, header_aliases: dict[str, str] | None = None
) -> list[dict[str, str | None]]:
    lines = [line for line in file_content.replace("\ufeff", "").splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    aliases = header_aliases or {}
    headers = [
        aliases.get(normalize_db_column_name(header), normalize_db_column_name(header))
        for header in lines[0].split("\t")
    ]
    rows: list[dict[str, str | None]] = []
    for line in lines[1:]:
        cells = line.split("\t")
        row: dict[str, str | None] = {}
        for index, header in enumerate(headers):
            row[header] = normalize_cell_value(cells[index] if index < len(cells) else None)
        rows.append(row)
    return rows


def build_summary_record(record: dict) -> dict:
    created_at = str(record.get("created_at") or "")
    updated_at = str(record.get("updated_at") or "")
    return {
        "fecVis": normalize_cell_value(record.get("fec_vis")),
        "nisRad": normalize_cell_value(record.get("nis_rad")),
        "nomRaz": normalize_cell_value(record.get("nom_raz")),
        "numOs": str(record.get("num_os")),
        "observac": normalize_cell_value(record.get("observac")),
        "ofComer": normalize_cell_value(record.get("of_comer")),
        "sourceFileName": normalize_cell_value(record.get("source_file_name")),
        "supervisionId": int(record.get("supervision_id")),
        "tipOs": normalize_cell_value(record.get("tip_os")),
        "updatedAt": updated_at,
        "wasEdited": created_at != "" and updated_at != "" and created_at != updated_at,
    }


async def get_supervision_schema(pool: AsyncConnectionPool) -> dict:
    return {"fields": await build_field_meta(pool)}


async def list_supervision_records(
    pool: AsyncConnectionPool,
    visit_date: str | None,
    search: str | None,
    *,
    user_role: str | None,
    user_id: int | None,
) -> list[dict]:
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return []

    params: list = []
    conditions: list[str] = []
    if visit_date:
        # `visit_date` ya llega en ISO (YYYY-MM-DD) desde el cliente. Se compara
        # directamente contra la fecha canonica derivada de fec_emis. OJO: no usar
        # format_visit_date_for_storage aqui -- devolveria DD/MM/YYYY y `%s::date`
        # lo interpretaria con DateStyle MDY (fecha equivocada).
        params.append(visit_date)
        conditions.append(f"{VISIT_DATE_SQL_NO_ALIAS} = %s::date")
    if search:
        params.append(f"%{search.strip().lower()}%")
        conditions.append(
            "lower(concat_ws(' ', num_os::text, nis_rad::text, nom_raz, tip_os, observac, source_file_name)) like %s"
        )
    if not is_admin(normalized_role):
        params.append(user_id)
        conditions.append("assigned_user_id = %s")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT
                  supervision_id,
                  num_os,
                  nis_rad,
                  nom_raz,
                  tip_os,
                  -- La lista web formatea `fecVis` como DD/MM/YYYY (formatRecordDate);
                  -- se emite en ese formato desde la fecha canonica (fec_emis).
                  to_char({VISIT_DATE_SQL_NO_ALIAS}, 'DD/MM/YYYY') AS fec_vis,
                  observac,
                  of_comer,
                  source_file_name,
                  created_at::text,
                  updated_at::text
                FROM public.supervision
                {where_clause}
                ORDER BY
                  source_imported_at DESC NULLS LAST,
                  CASE WHEN {VISIT_DATE_SQL_NO_ALIAS} IS NULL THEN 1 ELSE 0 END,
                  {VISIT_DATE_SQL_NO_ALIAS} DESC NULLS LAST,
                  num_os DESC;
                """,
                params,
            )
            rows = await cursor.fetchall()
    return [build_summary_record(row) for row in rows]


async def get_supervision_record(
    pool: AsyncConnectionPool,
    supervision_id: int,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict | None:
    fields = await build_field_meta(pool)
    normalized_role = normalize_role(user_role)
    if not is_admin(normalized_role) and user_id is None:
        return None

    conditions = ["supervision_id = %s"]
    params: list = [supervision_id]
    if not is_admin(normalized_role):
        params.append(user_id)
        conditions.append(f"assigned_user_id = %s")

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT * FROM public.supervision WHERE {' AND '.join(conditions)} LIMIT 1;",
                params,
            )
            row = await cursor.fetchone()
    if not row:
        return None
    data = {field["columnName"]: normalize_cell_value(row.get(field["columnName"])) for field in fields}
    return {
        "data": data,
        "fieldMeta": fields,
        "supervisionId": int(row["supervision_id"]),
    }


async def update_supervision_record(
    pool: AsyncConnectionPool,
    supervision_id: int,
    updates: dict[str, str | None],
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict | None:
    fields = await build_field_meta(pool)
    existing = await get_supervision_record(
        pool,
        supervision_id,
        user_role=user_role,
        user_id=user_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="No se encontro la actividad solicitada.")

    allowed_names = {field["columnName"] for field in fields if field["editable"]}
    update_entries = [(key, value) for key, value in updates.items() if key in allowed_names]
    if not update_entries:
        return existing

    assignments = ",\n            ".join(
        f"{quote_identifier(column_name)} = %s" for column_name, _ in update_entries
    )
    params = [normalize_cell_value(value) for _, value in update_entries] + [supervision_id]

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                UPDATE public.supervision
                SET
                  {assignments},
                  updated_at = NOW()
                WHERE supervision_id = %s;
                """,
                params,
            )
        await connection.commit()
    return await get_supervision_record(
        pool,
        supervision_id,
        user_role=user_role,
        user_id=user_id,
    )


async def import_supervision_txt_files(
    pool: AsyncConnectionPool,
    files: list[dict[str, str]],
    *,
    assigned_user_id: int,
    allow_reassignment: bool,
) -> dict:
    fields = await build_field_meta(pool)
    header_aliases = build_import_header_alias_map(fields)
    sedapal_columns = [field["columnName"] for field in fields if field["responsible"] == "SEDAPAL"]
    contractor_columns = [field["columnName"] for field in fields if field["responsible"] == "CONTRATISTA"]
    table_columns = {field["columnName"] for field in fields}
    technical_columns = {
        "supervision_id",
        "source_file_name",
        "source_imported_at",
        "created_at",
        "updated_at",
        "assigned_user_id",
    }
    # Every TXT header that maps to a business column must be persisted, even
    # when its database comment has no Responsable marker.
    importable_columns = [
        field["columnName"]
        for field in fields
        if field["columnName"] not in technical_columns
    ]

    file_results: list[dict] = []
    imported_visit_dates: set[str] = set()
    total_imported = 0
    total_skipped = 0
    fallback_visit_date = format_visit_date_for_storage(datetime.now().date().isoformat())

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            for file in files:
                rows = build_import_row_objects(file["content"], header_aliases)
                imported = 0
                skipped = 0

                for row in rows:
                    num_os = normalize_cell_value(row.get("num_os"))
                    if not num_os:
                        skipped += 1
                        continue
                    imported_visit_date = (
                        normalize_cell_value(row.get("fec_vis")) or fallback_visit_date
                    )
                    if not to_input_date_if_possible(imported_visit_date):
                        imported_visit_date = fallback_visit_date

                    candidate_columns = [
                        column_name
                        for column_name in importable_columns
                        if column_name != "num_os"
                        and column_name in table_columns
                        and column_name in row
                    ]
                    if "fec_vis" in table_columns and "fec_vis" not in candidate_columns:
                        candidate_columns.append("fec_vis")
                    sedapal_update_columns = [
                        column_name
                        for column_name in candidate_columns
                        if column_name in sedapal_columns
                    ]
                    contractor_update_columns = [
                        column_name
                        for column_name in candidate_columns
                        if column_name in contractor_columns
                    ]
                    unclassified_update_columns = [
                        column_name
                        for column_name in candidate_columns
                        if column_name not in sedapal_columns
                        and column_name not in contractor_columns
                    ]

                    insert_columns = list(
                        dict.fromkeys(
                            [
                                *candidate_columns,
                                "num_os",
                                "assigned_user_id",
                                "source_file_name",
                                "source_imported_at",
                            ]
                        )
                    )
                    values = []
                    for column_name in insert_columns:
                        if column_name == "num_os":
                            values.append(num_os)
                        elif column_name == "assigned_user_id":
                            values.append(assigned_user_id)
                        elif column_name == "source_file_name":
                            values.append(file["fileName"])
                        elif column_name == "source_imported_at":
                            values.append("NOW_PLACEHOLDER")
                        elif column_name == "fec_vis":
                            values.append(imported_visit_date)
                        else:
                            values.append(normalize_cell_value(row.get(column_name)))

                    placeholders = []
                    final_values = []
                    for value in values:
                        if value == "NOW_PLACEHOLDER":
                            placeholders.append("NOW()")
                        else:
                            placeholders.append("%s")
                            final_values.append(value)

                    update_assignments = [
                        f"{quote_identifier(column_name)} = excluded.{quote_identifier(column_name)}"
                        for column_name in sedapal_update_columns
                    ] + [
                        f"{quote_identifier(column_name)} = excluded.{quote_identifier(column_name)}"
                        for column_name in contractor_update_columns
                    ] + [
                        f"{quote_identifier(column_name)} = excluded.{quote_identifier(column_name)}"
                        for column_name in unclassified_update_columns
                    ] + [
                        "assigned_user_id = excluded.assigned_user_id",
                        "source_file_name = excluded.source_file_name",
                        "source_imported_at = excluded.source_imported_at",
                        "updated_at = NOW()",
                    ]

                    conflict_guard = (
                        ""
                        if allow_reassignment
                        else "WHERE public.supervision.assigned_user_id = excluded.assigned_user_id"
                    )
                    await cursor.execute(
                        f"""
                        INSERT INTO public.supervision ({", ".join(quote_identifier(column) for column in insert_columns)})
                        VALUES ({", ".join(placeholders)})
                        ON CONFLICT (num_os) DO UPDATE
                        SET {", ".join(update_assignments)}
                        {conflict_guard}
                        RETURNING supervision_id;
                        """,
                        final_values,
                    )
                    saved_row = await cursor.fetchone()
                    if not saved_row:
                        skipped += 1
                        continue

                    # La "fecha sugerida" que devolvemos al cliente debe coincidir con
                    # la fecha por la que la lista web filtra los registros: la canonica
                    # `fec_emis` (no `fec_vis`, que suele ser la fecha de importacion).
                    # Asi, tras importar, la pagina salta a la fecha donde el registro
                    # SI aparece, sin requerir refrescar manualmente. Fallback a fec_vis.
                    canonical_date = to_input_date_if_possible(
                        normalize_cell_value(row.get("fec_emis"))
                    ) or to_input_date_if_possible(imported_visit_date)
                    if canonical_date:
                        imported_visit_dates.add(canonical_date)
                    imported += 1

                file_results.append(
                    {"fileName": file["fileName"], "imported": imported, "skipped": skipped}
                )
                total_imported += imported
                total_skipped += skipped

        await connection.commit()

    sorted_dates = sorted(imported_visit_dates)
    return {
        "files": file_results,
        "importedVisitDates": sorted_dates,
        "suggestedDate": sorted_dates[-1] if sorted_dates else None,
        "totalImported": total_imported,
        "totalSkipped": total_skipped,
    }


_supervision_schema_ready = False
_supervision_media_schema_ready = False


async def ensure_supervision_field_location_columns(pool: AsyncConnectionPool) -> None:
    global _supervision_schema_ready
    if _supervision_schema_ready:
        return

    try:
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    ALTER TABLE public.supervision
                      ADD COLUMN IF NOT EXISTS verified_latitude numeric,
                      ADD COLUMN IF NOT EXISTS verified_longitude numeric,
                      ADD COLUMN IF NOT EXISTS verified_location_captured_at timestamptz;
                    """
                )
                await cursor.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'supervision_verified_latitude_range'
                      ) THEN
                        ALTER TABLE public.supervision
                          ADD CONSTRAINT supervision_verified_latitude_range
                          CHECK (verified_latitude IS NULL OR verified_latitude BETWEEN -90 AND 90);
                      END IF;

                      IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'supervision_verified_longitude_range'
                      ) THEN
                        ALTER TABLE public.supervision
                          ADD CONSTRAINT supervision_verified_longitude_range
                          CHECK (verified_longitude IS NULL OR verified_longitude BETWEEN -180 AND 180);
                      END IF;
                    END $$;
                    """
                )
            await connection.commit()
        _supervision_schema_ready = True
    except Exception as e:
        print(f"Warning: Non-fatal error while ensuring supervision columns: {e}")
        # Mark as ready so we don't repeatedly attempt the alter command
        _supervision_schema_ready = True


async def ensure_supervision_media_schema(pool: AsyncConnectionPool) -> None:
    global _supervision_media_schema_ready
    if _supervision_media_schema_ready:
        return

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            # Migracion one-time: la tabla vieja estaba keyed por `supervision_id`
            # (FK a la tabla `supervision` LOCAL). Ahora la supervision vive solo en
            # Supabase y las evidencias se guardan por `num_os` (work_order_number),
            # la unica clave estable compartida entre movil y BD local. Si existe la
            # forma vieja, se recrea (autorizado: no hay evidencias reales que preservar).
            await cursor.execute(
                """
                SELECT
                  bool_or(column_name = 'supervision_id') AS has_supervision_id,
                  bool_or(column_name = 'work_order_number') AS has_work_order_number
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'supervision_media';
                """
            )
            state = await cursor.fetchone()
            if state and state.get("has_supervision_id") and not state.get("has_work_order_number"):
                await cursor.execute("DROP TABLE public.supervision_media;")

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.supervision_media (
                  id bigserial PRIMARY KEY,
                  work_order_number text NOT NULL,
                  media_type text NOT NULL CHECK (media_type IN ('photo', 'video')),
                  media_path text NOT NULL,
                  mime_type text,
                  description text,
                  captured_at timestamptz NOT NULL DEFAULT NOW(),
                  created_at timestamptz NOT NULL DEFAULT NOW()
                );
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_supervision_media_work_order
                  ON public.supervision_media (work_order_number, captured_at DESC);
                """
            )
            # Reintentos de subida desde el movil (conexion inestable en campo) podian
            # insertar la misma foto/video dos veces cuando el nombre de archivo se
            # reutilizaba (ver build_sequential_media_filename). Esos duplicados se
            # adjuntaban de nuevo en el correo de derivacion. El indice unico bloquea
            # nuevos duplicados; register_supervision_media usa ON CONFLICT DO NOTHING.
            await cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_supervision_media_work_order_path
                  ON public.supervision_media (work_order_number, media_path);
                """
            )
            # Coordenadas de captura (auditoria legal), tomadas del dispositivo al
            # momento de tomar la foto/video, no al sincronizar.
            await cursor.execute(
                """
                ALTER TABLE public.supervision_media
                  ADD COLUMN IF NOT EXISTS latitude numeric,
                  ADD COLUMN IF NOT EXISTS longitude numeric;
                """
            )
        await connection.commit()

    _supervision_media_schema_ready = True



async def get_supervision_access(
    pool: AsyncConnectionPool,
    supervision_id: int,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    normalized_role = normalize_role(user_role)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT supervision_id, assigned_user_id, num_os::text AS num_os, nis_rad
                FROM public.supervision
                WHERE supervision_id = %s
                LIMIT 1;
                """,
                (supervision_id,),
            )
            row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro la actividad solicitada.")

    if not is_admin(normalized_role) and (
        user_id is None or row.get("assigned_user_id") != user_id
    ):
        raise HTTPException(status_code=403, detail="No autorizado.")

    return row


async def get_supervision_record_access(
    pool: AsyncConnectionPool,
    work_order_number: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    # Resuelve acceso y el `supervision_id` (BD local, donde vive supervision_media)
    # directamente a partir de `work_order_number` -- la unica clave estable y
    # compartida entre el flujo movil (que puede leer de Supabase) y el
    # almacenamiento de evidencias (siempre en BD local). Ya no depende de
    # `supervision_records`: esa tabla se elimino, `num_os` es unico en `supervision`.
    #
    # work_order_number puede ser:
    # - num_os como string para supervisiones con OS (p.ej. "12345")
    # - "id-{supervision_id}" para supervisiones sin OS (p.ej. "id-789")
    # - "planilla-{id}" para evidencias de planillas
    # - nis_rad (supply code) para supervisiones sin OS (fallback)
    normalized_role = normalize_role(user_role)

    planilla_key = work_order_number.strip()
    if planilla_key.startswith("planilla-"):
        try:
            planilla_id = int(planilla_key[len("planilla-"):])
        except ValueError:
            raise HTTPException(status_code=404, detail="No se encontro el registro de la planilla.")
        return await get_planilla_record_access(
            pool,
            planilla_id,
            user_role=normalized_role,
            user_id=user_id,
        )

    row = None

    # Primero intentar buscar por num_os (supervisiones con OS)
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT supervision_id, assigned_user_id, num_os::text AS num_os, nis_rad, created_at
                FROM public.supervision
                WHERE num_os::text = %s
                LIMIT 1;
                """,
                (work_order_number,),
            )
            row = await cursor.fetchone()

    # Si no encontro por num_os, intentar buscar por supervision_id (formato "id-{id}")
    if not row:
        supervision_id_match = work_order_number.strip()
        if supervision_id_match.startswith("id-"):
            try:
                supervision_id = int(supervision_id_match[3:])
                async with pool.connection() as connection:
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            """
                            SELECT supervision_id, assigned_user_id, num_os::text AS num_os, nis_rad, created_at
                            FROM public.supervision
                            WHERE supervision_id = %s
                            LIMIT 1;
                            """,
                            (supervision_id,),
                        )
                        row = await cursor.fetchone()
            except (ValueError, IndexError):
                pass

    # Ultimo intento: buscar por nis_rad (supply code)
    if not row:
        async with pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT supervision_id, assigned_user_id, num_os::text AS num_os, nis_rad, created_at
                    FROM public.supervision
                    WHERE nis_rad = %s
                    LIMIT 1;
                    """,
                    (work_order_number,),
                )
                row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontro el registro de supervision.")

    if not is_admin(normalized_role) and (
        user_id is None or row.get("assigned_user_id") != user_id
    ):
        raise HTTPException(status_code=403, detail="No autorizado.")

    return row


async def get_supervision_croquis_context(
    pool: AsyncConnectionPool,
    supply_pool: AsyncConnectionPool,
    supervision_id: int,
    supply_code: str | None,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    await ensure_supervision_field_location_columns(pool)
    await get_supervision_access(
        pool,
        supervision_id,
        user_role=user_role,
        user_id=user_id,
    )

    normalized_supply_code = normalize_cell_value(supply_code)

    base_row = None
    if normalized_supply_code:
        # `supervision` is intentionally read through the Supabase boundary,
        # while the cadastral supply catalog lives in the local GIS database.
        # Do not use the former connection for `customer_supplies`: its role
        # has no access to the local catalog.
        async with supply_pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT
                      coalesce(geolocation_address, service_address) AS address,
                      latitude,
                      longitude,
                      supply_code
                    FROM public.customer_supplies
                    WHERE supply_code = %s
                    LIMIT 1;
                    """,
                    (normalized_supply_code,),
                )
                base_row = await cursor.fetchone()

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT
                  verified_latitude,
                  verified_longitude,
                  verified_location_captured_at::text AS verified_location_captured_at
                FROM public.supervision
                WHERE supervision_id = %s
                LIMIT 1;
                """,
                (supervision_id,),
            )
            verified_row = await cursor.fetchone()

    base_location = None
    if base_row:
        base_location = {
            "address": normalize_cell_value(base_row.get("address")),
            "latitude": normalize_coordinate(base_row.get("latitude")),
            "longitude": normalize_coordinate(base_row.get("longitude")),
            "supplyCode": normalize_cell_value(base_row.get("supply_code")),
        }

    verified_location = None
    if verified_row and (
        verified_row.get("verified_latitude") is not None
        or verified_row.get("verified_longitude") is not None
    ):
        verified_location = {
            "address": None,
            "capturedAt": normalize_cell_value(verified_row.get("verified_location_captured_at")),
            "latitude": normalize_coordinate(verified_row.get("verified_latitude")),
            "longitude": normalize_coordinate(verified_row.get("verified_longitude")),
        }

    return {
        "baseLocation": base_location,
        "verifiedLocation": verified_location,
    }


async def save_supervision_verified_location(
    pool: AsyncConnectionPool,
    supervision_id: int,
    latitude: float,
    longitude: float,
    *,
    user_role: str | None,
    user_id: int | None,
) -> dict:
    await ensure_supervision_field_location_columns(pool)
    await get_supervision_access(
        pool,
        supervision_id,
        user_role=user_role,
        user_id=user_id,
    )

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE public.supervision
                SET
                  verified_latitude = %s,
                  verified_longitude = %s,
                  verified_location_captured_at = NOW(),
                  updated_at = NOW()
                WHERE supervision_id = %s
                RETURNING
                  verified_latitude,
                  verified_longitude,
                  verified_location_captured_at::text AS verified_location_captured_at;
                """,
                (latitude, longitude, supervision_id),
            )
            row = await cursor.fetchone()
        await connection.commit()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No se encontro la actividad para guardar la ubicacion verificada.",
        )

    return {
        "address": None,
        "capturedAt": normalize_cell_value(row.get("verified_location_captured_at")),
        "latitude": normalize_coordinate(row.get("verified_latitude")),
        "longitude": normalize_coordinate(row.get("verified_longitude")),
    }


async def list_supervision_photo_rows(
    pool: AsyncConnectionPool,
    work_order_number: str,
) -> list[dict]:
    # El acceso ya se valida en el router contra Supabase (fuente de verdad de la
    # supervision). Aqui solo se leen las fotos de la BD local por num_os. Ya no se
    # hace JOIN a `public.supervision` local (que puede no tener el registro cuando
    # la supervision solo existe en Supabase).
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT cip.id::text, cip.photo_path, cip.description, cip.captured_at::text
                FROM public.commercial_inspection_photos cip
                JOIN public.commercial_inspections ci ON ci.id = cip.inspection_id
                WHERE ci.work_order_number = %s
                ORDER BY cip.captured_at DESC;
                """,
                (work_order_number,),
            )
            return await cursor.fetchall()


async def list_supervision_media_rows(
    pool: AsyncConnectionPool,
    work_order_number: str,
) -> list[dict]:
    # El acceso ya se valida en el router contra Supabase (fuente de verdad de la
    # supervision). Aqui solo se leen las evidencias de la BD local por `num_os`.
    #
    # El resultado se acota al ultimo dia calendario (zona America/Lima) en el que
    # se capturo evidencia bajo esta clave. Sin este filtro, restos de un intento de
    # captura abandonado en un dia anterior bajo la misma clave (ej. una supervision
    # retomada al dia siguiente) se seguian devolviendo junto con la evidencia real,
    # e inflaban el correo de derivacion con enlaces que no correspondian a la visita.
    #
    # Antes se ancoraba a `supervision.created_at` (fecha de registro/creacion de la
    # fila en Supabase), asumiendo que coincidia con el dia de la visita. Esa premisa
    # es falsa cuando la supervision se crea (importada u OS pre-registrada) uno o
    # mas dias antes de que el inspector realmente visite y capture evidencia: el
    # filtro descartaba entonces TODA la evidencia real (ningun `captured_at` caia en
    # el dia de `created_at`), dejando el correo de derivacion sin enlaces pese a
    # tener fotos/videos subidos.
    await ensure_supervision_media_schema(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                WITH latest_day AS (
                  SELECT MAX((captured_at AT TIME ZONE 'America/Lima')::date) AS day
                  FROM public.supervision_media
                  WHERE work_order_number = %s
                )
                SELECT
                  m.id::text,
                  m.media_type,
                  m.media_path,
                  m.mime_type,
                  m.description,
                  m.captured_at::text,
                  m.latitude,
                  m.longitude
                FROM public.supervision_media m, latest_day
                WHERE m.work_order_number = %s
                  AND (
                    latest_day.day IS NULL
                    OR (m.captured_at AT TIME ZONE 'America/Lima')::date = latest_day.day
                  )
                ORDER BY m.captured_at DESC, m.id DESC;
                """,
                (work_order_number, work_order_number),
            )
            return await cursor.fetchall()


async def register_supervision_media(
    pool: AsyncConnectionPool,
    work_order_number: str,
    media_type: str,
    media_path: str,
    mime_type: str | None,
    description: str | None,
    captured_at: datetime | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict:
    # El acceso ya se valida en el router contra Supabase (fuente de verdad de la
    # supervision). Aqui solo se persiste la evidencia en la BD local por `num_os`.
    await ensure_supervision_media_schema(pool)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            # ON CONFLICT DO NOTHING: si el mismo (work_order_number, media_path) ya
            # fue registrado por un intento anterior (reintento de subida desde el
            # movil), no crea una fila duplicada que luego se adjuntaria dos veces en
            # el correo de derivacion.
            await cursor.execute(
                """
                INSERT INTO public.supervision_media (
                  work_order_number,
                  media_type,
                  media_path,
                  mime_type,
                  description,
                  captured_at,
                  latitude,
                  longitude
                )
                VALUES (%s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()), %s, %s)
                ON CONFLICT (work_order_number, media_path) DO NOTHING
                RETURNING
                  id::text,
                  media_type,
                  media_path,
                  mime_type,
                  description,
                  captured_at::text,
                  latitude,
                  longitude;
                """,
                (
                    work_order_number,
                    normalize_cell_value(media_type),
                    media_path,
                    normalize_cell_value(mime_type),
                    normalize_cell_value(description),
                    captured_at,
                    latitude,
                    longitude,
                ),
            )
            row = await cursor.fetchone()

            if not row:
                # Conflicto: (work_order_number, media_path) ya existia (reintento de
                # subida). No es un error - se devuelve la fila ya registrada para que
                # el cliente reciba una respuesta exitosa e idempotente.
                await cursor.execute(
                    """
                    SELECT
                      id::text,
                      media_type,
                      media_path,
                      mime_type,
                      description,
                      captured_at::text,
                      latitude,
                      longitude
                    FROM public.supervision_media
                    WHERE work_order_number = %s AND media_path = %s
                    LIMIT 1;
                    """,
                    (work_order_number, media_path),
                )
                row = await cursor.fetchone()

        await connection.commit()

    if not row:
        raise HTTPException(status_code=500, detail="No se pudo registrar la evidencia de supervision.")

    return row


async def register_supervision_photo(
    pool: AsyncConnectionPool,
    work_order_number: str,
    supply_code: str | None,
    photo_path: str,
    description: str | None,
) -> dict:
    # El acceso ya se valida en el router contra Supabase. Aqui solo se persiste la
    # foto en la BD local (commercial_inspections / commercial_inspection_photos),
    # keyed por num_os (work_order_number).
    if not work_order_number:
        raise HTTPException(status_code=400, detail="La actividad no tiene orden de trabajo asociada.")

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT id::text
                FROM public.commercial_inspections
                WHERE work_order_number = %s
                LIMIT 1;
                """,
                (work_order_number,),
            )
            inspection_row = await cursor.fetchone()

            if inspection_row:
                inspection_id = inspection_row["id"]
            else:
                source_hash = f"placeholder_{work_order_number}"
                await cursor.execute(
                    """
                    INSERT INTO public.commercial_inspections (
                      source_hash,
                      source_file,
                      source_year,
                      source_category,
                      is_fuente_propia,
                      work_order_number,
                      supply_code,
                      raw_data
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (source_hash) DO UPDATE SET updated_at = NOW()
                    RETURNING id::text;
                    """,
                    (
                        source_hash,
                        "placeholder_supervision",
                        datetime.now().year,
                        "placeholder",
                        False,
                        work_order_number,
                        supply_code,
                        "{}",
                    ),
                )
                inspection_row = await cursor.fetchone()
                inspection_id = inspection_row["id"]

            await cursor.execute(
                """
                INSERT INTO public.commercial_inspection_photos (inspection_id, photo_path, description)
                VALUES (%s, %s, %s)
                RETURNING id::text, photo_path, description, captured_at::text;
                """,
                (int(inspection_id), photo_path, normalize_cell_value(description)),
            )
            photo_row = await cursor.fetchone()
        await connection.commit()

    if not photo_row:
        raise HTTPException(status_code=500, detail="No se pudo registrar la foto de la inspeccion.")

    return photo_row


async def delete_supervision_photo(
    pool: AsyncConnectionPool,
    photo_id: int,
) -> dict:
    # El acceso ya se valida en el router contra Supabase. Aqui solo se borra la
    # foto en la BD local por su id.
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT photo_path
                FROM public.commercial_inspection_photos
                WHERE id = %s;
                """,
                (photo_id,),
            )
            photo_row = await cursor.fetchone()

            if photo_row:
                await cursor.execute(
                    """
                    DELETE FROM public.commercial_inspection_photos
                    WHERE id = %s;
                    """,
                    (photo_id,),
                )
        await connection.commit()

    return {
        "photoPath": photo_row.get("photo_path") if photo_row else None,
        "success": True,
    }


# Orden de columnas del formato legado de interconexion (mismo header que los TXT
# fuente que se importan). NO es el orden fisico de la tabla (ordinal_position),
# que refleja el historial de ALTER TABLE ADD COLUMN y no coincide con el
# formato original. observm1_ext (campo extendido 900, auxiliar de observm1) no
# forma parte de este formato a proposito: el trigger de la BD mantiene
# observm1 (250) en sync, y ese es el campo que se exporta.
LEGACY_SUPERVISION_EXPORT_COLUMNS = [
    "of_comer", "gen_por", "cus", "ruta", "itin", "tipsum", "fec_emis", "observac",
    "nom_raz", "le_ruc", "telf_fax", "cod_abas", "refer", "cua", "medidor", "diam",
    "ult_lec", "pto_med", "acom", "nis_rad", "num_os", "tip_os", "fe_res", "cod_emp",
    "fec_vis", "co_accej", "serv", "serv_dt", "caja", "est_caja", "cnx_con", "cx_c_dt",
    "lec", "fun_med1", "fun_med2", "fun_med3", "precint", "fuga_caj", "n_conex", "med2",
    "med3", "niple2", "niple3", "valvulas", "disposit", "clandes", "niv_finc", "estado",
    "n_habit", "uu_ocup", "soc_ocup", "dom_ocup", "com_ocup", "ind_ocup", "est_ocup", "uu_docup",
    "soc_desc", "dom_desc", "com_desc", "ind_desc", "est_desc", "uso_unic", "calle", "numero",
    "duplicad", "mza", "lote", "cgv", "urbaniza", "distrito", "calle_c", "num_c",
    "dupli_c", "mza_c", "lote_c", "urb_c", "ref_c", "dist_c", "atendio", "paterno",
    "materno", "nombre", "razsoc", "dni", "telef", "fax", "ruc", "inod_b",
    "inod_f", "inod_cl", "lava_b", "lava_f", "lava_cl", "duch_b", "duch_f", "duch_cl",
    "urin_b", "urin_f", "urin_cl", "grifo_b", "grifo_f", "grifo_cl", "ciste_b", "ciste_f",
    "ciste_cl", "tanqu_b", "tanqu_f", "tanqu_cl", "pisc_b", "pisc_f", "pisc_cl", "bidet_b",
    "bidet_f", "bidet_cl", "abast", "ptome_ca", "num_p", "dup_p", "mza_p", "lote_p",
    "observ", "observm1", "observm2", "cota_sum_c", "pisos_c", "cod_ubic_c", "fte_conexion_c", "cup_c",
    "cota_sum", "pisos", "cod_ubic", "fte_conexion", "cup", "devuelto", "fecha_d", "prior",
    "actualiza", "fecha_c", "resuelto", "multi", "parcial", "fte_cnx", "cota_sumc", "pisosc",
    "cod_ubicc", "fte_cnxc", "cupc", "hora_i", "hora_f", "valv_e_tlsc", "mat_valv_e", "valv_s_p",
    "mat_valv_s", "sector", "cup_pred", "aol", "ciclo", "nreclamo", "celular_cli", "fax_cli",
    "niv_predio", "nse_pre", "soc_ocup_cat", "dom_ocup_cat", "com_ocup_cat", "ind_ocup_cat", "est_ocup_cat", "soc_desoc_cat",
    "dom_desoc_cat", "com_desoc_cat", "ind_desoc_cat", "est_desoc_cat", "est_sum", "fte_conex", "ubic_pred", "dia_conex",
    "cota_conex", "disp_seg", "tip_lec", "hor_abasc", "nia", "dia_alcan", "fte_alcan", "ubic_alcan",
    "tip_suelo_alcan", "tip_descar_alcan", "cotah_alcan", "cotav_alcan", "long_alcan", "profund_alcan", "tr_grasas", "sistrat",
    "mat_tapa_alcan", "est_tapa_alcan", "mat_caja_alcan", "est_caja_alcan", "mat_tub_alcan", "est_tub_alcan", "email", "cel_cli_c",
    "telf_emp", "piso_dpto_f", "sector_f", "fte_finca", "nse_f", "obs_f", "corr_cup", "pred_no_ubic",
    "var_act_predio", "inspec_realiz", "mot_no_ing", "dia_conex_agua", "imposibilidad", "seguro_tap", "est_tapa", "inci_medidor",
    "acceso_inmueb", "obs_atendio", "cartografia", "mnto_cartog", "corr_cup_f", "num_med", "dia_med", "cnx2_con",
    "cnx3_con", "dia_med2", "dia_med3", "lec_med2", "lec_med3", "cota_hor_des", "cota_ver_des", "long_cnx_des",
    "prof_cja_des", "tip_suelo_des", "ind_tram_des", "dia_conex_des", "silo", "est_tapa_des", "est_caja_des", "est_tub_des",
    "tip_mat_tap_d", "tip_mat_caj_d", "tip_mat_tub_d", "sis_trat_res", "tip_des_des", "clandes_des", "cod_sup", "cod_digit",
]


async def export_supervision_txt(
    pool: AsyncConnectionPool,
    start: str,
    end: str,
    *,
    user_role: str | None,
    user_id: int | None,
) -> str:
    normalized_role = normalize_role(user_role)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'supervision';
                """
            )
            existing_columns = {row["column_name"] for row in await cursor.fetchall()}

            # El header/orden de columnas del export es el formato legado fijo, no el
            # orden fisico de la tabla (que cambia con cada ALTER TABLE ADD COLUMN).
            # Se filtra contra las columnas existentes por robustez ante drift de
            # esquema, sin alterar el orden esperado por el sistema de interconexion.
            ordered_columns = [
                column for column in LEGACY_SUPERVISION_EXPORT_COLUMNS if column in existing_columns
            ]
            if not is_admin(normalized_role) and user_id is None:
                return "\r\n".join(["\t".join(ordered_columns)])

            conditions = [f"{VISIT_DATE_SQL_NO_ALIAS} between %s::date and %s::date"]
            params: list = [start, end]

            if not is_admin(normalized_role):
                params.append(user_id)
                conditions.append(f"assigned_user_id = %s")

            await cursor.execute(
                f"""
                SELECT {", ".join(quote_identifier(column) for column in ordered_columns)}
                FROM public.supervision
                WHERE {" AND ".join(conditions)}
                ORDER BY
                  {VISIT_DATE_SQL_NO_ALIAS} ASC,
                  num_os ASC;
                """,
                params,
            )
            rows = await cursor.fetchall()

    lines = [
        "\t".join(ordered_columns),
        *[
            "\t".join(
                str(row.get(column) or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
                for column in ordered_columns
            )
            for row in rows
        ],
    ]
    return "\r\n".join(lines)
