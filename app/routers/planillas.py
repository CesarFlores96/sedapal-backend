from datetime import date, datetime
from pathlib import Path as FsPath

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Path, Query, UploadFile
from pydantic import BaseModel, Field

from app.database import get_pool, get_supabase_pool
from app.media_storage import (
    build_sequential_media_filename,
    embed_photo_metadata,
    sanitize_folder_segment,
    supervision_media_dir,
    supervision_media_url,
)
from app.repositories.planillas import (
    assign_planilla_user,
    complete_planilla,
    get_planilla_record_access,
    get_planilla_row,
    insert_planilla_rows,
    list_planillas_by_day,
    mark_planilla_derivation_sent,
    update_planilla_observaciones,
)
from app.repositories.supervision_table import register_supervision_media
from app.supabase_auth import resolve_actor

router = APIRouter(tags=["planillas"])


def _planilla_pool():
    try:
        return get_supabase_pool("planillas")
    except RuntimeError:
        return get_pool()


async def _actor(
    authorization: str | None,
    user_role: str | None,
    user_id: str | None,
) -> tuple[str | None, int | None]:
    return await resolve_actor(get_pool(), authorization, user_role, parse_user_id(user_id))


class ObservacionesRequest(BaseModel):
    observaciones: str | None = Field(default=None, max_length=10_000)
    lectura: str | None = Field(default=None, max_length=20)


class AssignUserRequest(BaseModel):
    userId: int = Field(ge=1)


class NewPlanillaRow(BaseModel):
    fecha: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    nis: str = Field(default="", max_length=80)
    ruta: str | None = None
    itin: str | None = None
    aol: str | None = None
    medidor: str | None = None
    direccion: str | None = None
    distrito: str | None = None
    cliente: str | None = None
    ciclo: str | None = None
    supervisor: str | None = None
    areaSolicitante: str | None = None
    carga: str | None = None
    assignedUserId: int | None = None
    sourceFileName: str
    uploadedByUserId: str
    uploadedByName: str | None = None


class BulkInsertRequest(BaseModel):
    rows: list[NewPlanillaRow] = Field(min_length=1)

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": (".jpg", "photo"),
    "image/png": (".png", "photo"),
    "image/webp": (".webp", "photo"),
    "video/mp4": (".mp4", "video"),
    "video/quicktime": (".mov", "video"),
    "video/webm": (".webm", "video"),
}


def parse_user_id(user_id: str | None) -> int | None:
    return int(user_id) if user_id and user_id.strip().isdigit() else None


def _parse_captured_at(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_planilla_date(raw_value: object) -> date:
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    try:
        return date.fromisoformat(str(raw_value).strip()[:10])
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail="La planilla no tiene una fecha operativa valida.",
        ) from error


@router.post("/planillas/{planilla_id}/media")
async def post_planilla_media(
    planilla_id: int = Path(..., ge=1),
    file: UploadFile = File(...),
    description: str | None = Form(default=None),
    media_type: str = Form(..., alias="mediaType"),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    captured_at: str | None = Form(default=None, alias="capturedAt"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    media_pool = get_pool()

    # Intenta resolver la base remota para auth, si no usa la local
    try:
        auth_pool = get_supabase_pool("planillas")
    except RuntimeError:
        auth_pool = media_pool

    normalized_media_type = media_type.strip().lower()
    if normalized_media_type not in {"photo", "video"}:
        raise HTTPException(status_code=400, detail="Tipo de evidencia no valido.")

    allowed_media = ALLOWED_MEDIA_TYPES.get(file.content_type or "")
    if not allowed_media:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido.")

    extension, detected_media_type = allowed_media
    if detected_media_type != normalized_media_type:
        raise HTTPException(status_code=400, detail="El tipo de evidencia no coincide con el archivo enviado.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo recibido esta vacio.")

    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo excede el limite permitido de 200 MB.")

    access = await get_planilla_record_access(
        auth_pool,
        planilla_id,
        user_role=role,
        user_id=actor_id,
    )

    parsed_captured_at = _parse_captured_at(captured_at)

    if normalized_media_type == "photo":
        content = embed_photo_metadata(
            content,
            extension,
            latitude=latitude,
            longitude=longitude,
            captured_at=parsed_captured_at,
        )

    # El identificador para el archivo
    work_order_number = f"planilla-{planilla_id}"
    supply_code = str(access.get("nis_rad") or work_order_number)
    planilla_date = _parse_planilla_date(access.get("fecha"))
    target_dir = supervision_media_dir(work_order_number, storage_date=planilla_date)

    target_path: FsPath | None = None
    file_name = ""
    for _ in range(5):
        candidate_name = build_sequential_media_filename(target_dir, supply_code, extension)
        candidate_path = target_dir / candidate_name
        try:
            with candidate_path.open("xb") as handle:
                handle.write(content)
            file_name = candidate_name
            target_path = candidate_path
            break
        except FileExistsError:
            continue

    if target_path is None:
        raise HTTPException(status_code=500, detail="No se pudo generar un nombre de archivo unico.")

    try:
        media_path = supervision_media_url(
            work_order_number,
            file_name,
            storage_date=planilla_date,
        )
        # Reutilizamos el repo de media existente que es comun para evidencias
        return await register_supervision_media(
            media_pool,
            work_order_number,
            normalized_media_type,
            media_path,
            file.content_type,
            description,
            captured_at=parsed_captured_at,
            latitude=latitude,
            longitude=longitude,
        )
    except Exception:
        target_path.unlink(missing_ok=True)
        raise


@router.post("/planillas/pdf")
async def post_planilla_pdf(
    file: UploadFile = File(...),
    carga: str = Form(...),
    fecha_iso: str = Form(..., alias="fechaIso"),
) -> dict:
    """Guarda la 'Planilla de Verificacion' ya generada por la web en la misma
    carpeta OneDrive del dia que usa la evidencia, para que quede disponible
    para el equipo (y consumible por movil) ademas de ir adjunta al correo."""

    content = await file.read()
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="El archivo no es un PDF valido.")

    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El PDF excede el limite permitido de 50 MB.")

    planilla_date = _parse_planilla_date(fecha_iso)
    identifier = f"planilla-{carga}"
    file_name = f"{sanitize_folder_segment(f'planilla_{carga}_{fecha_iso}')}.pdf"
    target_dir = supervision_media_dir(identifier, storage_date=planilla_date)
    (target_dir / file_name).write_bytes(content)

    return {
        "filename": file_name,
        "url": supervision_media_url(
            identifier,
            file_name,
            storage_date=planilla_date,
        ),
    }


@router.get("/planillas/{planilla_id}")
async def get_planilla_by_id(
    planilla_id: int = Path(..., ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await get_planilla_row(
        _planilla_pool(),
        planilla_id,
        user_role=role,
        user_id=actor_id,
    )


@router.patch("/planillas/{planilla_id}/derivation-sent")
async def patch_planilla_derivation_sent(
    planilla_id: int = Path(..., ge=1),
) -> dict:
    await mark_planilla_derivation_sent(_planilla_pool(), planilla_id)
    return {"success": True}


@router.get("/planillas")
async def get_planillas_by_day(
    fecha: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    # `fecha` es opcional: la web siempre lo manda (vista por dia); movil lo
    # omite para listar todas sus planillas de una vez y agruparlas por dia
    # en el cliente (ver app/repositories/planillas.py::list_planillas_by_day).
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await list_planillas_by_day(
        _planilla_pool(),
        fecha,
        user_role=role,
        user_id=actor_id,
    )


@router.patch("/planillas/{planilla_id}/observaciones")
async def patch_planilla_observaciones(
    planilla_id: int = Path(..., ge=1),
    payload: ObservacionesRequest = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await update_planilla_observaciones(
        _planilla_pool(),
        planilla_id,
        payload.observaciones,
        lectura=payload.lectura,
        user_role=role,
        user_id=actor_id,
    )


@router.patch("/planillas/{planilla_id}/assign")
async def patch_planilla_assign(
    planilla_id: int = Path(..., ge=1),
    payload: AssignUserRequest = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    role, _ = await _actor(authorization, user_role, None)
    return await assign_planilla_user(
        _planilla_pool(),
        planilla_id,
        payload.userId,
        user_role=role,
    )


@router.post("/planillas/{planilla_id}/complete")
async def post_planilla_complete(
    planilla_id: int = Path(..., ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    role, actor_id = await _actor(authorization, user_role, user_id)
    return await complete_planilla(
        _planilla_pool(),
        planilla_id,
        user_role=role,
        user_id=actor_id,
    )


@router.post("/planillas/bulk")
async def post_planillas_bulk(
    payload: BulkInsertRequest = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> list[dict]:
    role, _ = await _actor(authorization, user_role, None)
    if role and role.strip().lower() not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Solo un administrador puede subir planillas.")

    rows = [
        {
            "fecha": row.fecha,
            "nis": row.nis,
            "ruta": row.ruta,
            "itin": row.itin,
            "aol": row.aol,
            "medidor": row.medidor,
            "direccion": row.direccion,
            "distrito": row.distrito,
            "cliente": row.cliente,
            "ciclo": row.ciclo,
            "supervisor": row.supervisor,
            "area_solicitante": row.areaSolicitante,
            "carga": row.carga,
            "assigned_user_id": row.assignedUserId,
            "source_file_name": row.sourceFileName,
            "uploaded_by_user_id": row.uploadedByUserId,
            "uploaded_by_name": row.uploadedByName,
        }
        for row in payload.rows
    ]
    return await insert_planilla_rows(_planilla_pool(), rows)
