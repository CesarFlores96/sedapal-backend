from datetime import date, datetime
from pathlib import Path as FsPath

from fastapi import APIRouter, File, Form, Header, HTTPException, Path, UploadFile

from app.database import get_pool, get_supabase_pool
from app.media_storage import (
    build_sequential_media_filename,
    embed_photo_metadata,
    sanitize_folder_segment,
    supervision_media_dir,
    supervision_media_url,
)
from app.repositories.planillas import get_planilla_record_access
from app.repositories.supervision_table import register_supervision_media

router = APIRouter(tags=["planillas"])

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
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
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
        user_role=user_role,
        user_id=parse_user_id(user_id),
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
