from datetime import datetime
from pathlib import Path as FsPath

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.authz import ensure_path_access, is_admin
from app.database import get_gis_pool, get_pool, get_supabase_pool
from app.media_storage import (
    build_sequential_media_filename,
    embed_photo_metadata,
    find_supervision_signed_pdf,
    supervision_media_dir,
    supervision_media_url,
    supervision_signed_pdf_path,
)
from app.repositories.supervision_table import (
    ensure_supervision_media_schema,
    create_supervision_draft,
    delete_supervision_photo,
    export_supervision_txt,
    get_supervision_access,
    get_supervision_record,
    get_supervision_record_access,
    get_supervision_croquis_context,
    get_supervision_schema,
    import_supervision_txt_files,
    list_completed_supervisions_for_bulk_notice,
    list_supervision_media_rows,
    list_supervision_records,
    list_supervision_photo_rows,
    mark_supervisions_exported,
    register_supervision_media,
    register_supervision_photo,
    restore_supervision,
    save_supervision_verified_location,
    soft_delete_supervision,
    update_supervision_record,
)


class SupervisionUpdateRequest(BaseModel):
    updates: dict[str, str | None] = Field(default_factory=dict)


class ImportedFilePayload(BaseModel):
    content: str
    fileName: str = Field(min_length=1, max_length=255)


class SupervisionImportRequest(BaseModel):
    files: list[ImportedFilePayload] = Field(min_length=1)
    assignedUserId: int | None = Field(default=None, ge=1)


class VerifiedLocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class SupervisionPhotoCreateRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    photoPath: str = Field(min_length=1, max_length=1000)


ALLOWED_MEDIA_TYPES = {
    "image/jpeg": (".jpg", "photo"),
    "image/png": (".png", "photo"),
    "image/webp": (".webp", "photo"),
    "video/mp4": (".mp4", "video"),
    "video/quicktime": (".mov", "video"),
    "video/webm": (".webm", "video"),
}

MAX_SIGNED_PDF_BYTES = 15 * 1024 * 1024


def parse_user_id(user_id: str | None) -> int | None:
    return int(user_id) if user_id and user_id.strip().isdigit() else None


router = APIRouter(tags=["supervision"])


def get_supervision_pool():
    try:
        return get_supabase_pool("supervision")
    except RuntimeError:
        return get_pool()


@router.get("/supervision-schema")
async def get_schema() -> dict:
    return await get_supervision_schema(get_supervision_pool())


@router.get("/supervision-records")
async def get_records(
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    search: str | None = Query(default=None, min_length=1, max_length=120),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    return await list_supervision_records(
        get_supervision_pool(),
        date,
        search,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )


@router.get("/supervision-records/export-txt")
async def get_records_export_txt(
    start: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> PlainTextResponse:
    if start > end:
        raise HTTPException(status_code=400, detail="La fecha inicial no puede ser mayor que la fecha final.")

    content = await export_supervision_txt(
        get_supervision_pool(),
        start,
        end,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f'attachment; filename="supervisiones-{start}-a-{end}.txt"',
        },
    )


@router.get("/supervision-records/{record_id}")
async def get_record(
    record_id: int = Path(..., ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict | None:
    return await get_supervision_record(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )


@router.patch("/supervision-records/{record_id}")
async def patch_record(
    record_id: int = Path(..., ge=1),
    payload: SupervisionUpdateRequest = Body(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict | None:
    return await update_supervision_record(
        get_supervision_pool(),
        record_id,
        payload.updates,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )


@router.get("/supervision-records/{record_id}/croquis-context")
async def get_record_croquis_context(
    record_id: int = Path(..., ge=1),
    supply_code: str | None = Query(default=None, alias="supplyCode", max_length=80),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    return await get_supervision_croquis_context(
        get_supervision_pool(),
        get_gis_pool(),
        record_id,
        supply_code,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )


@router.patch("/supervision-records/{record_id}/croquis-context")
async def patch_record_croquis_context(
    record_id: int = Path(..., ge=1),
    payload: VerifiedLocationRequest = Body(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    return await save_supervision_verified_location(
        get_supervision_pool(),
        record_id,
        payload.latitude,
        payload.longitude,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )


@router.get("/supervision-records/{record_id}/photos")
async def get_record_photos(
    record_id: int = Path(..., ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    # Acceso contra Supabase (fuente de verdad); fotos en BD local por num_os.
    access = await get_supervision_access(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    return await list_supervision_photo_rows(
        get_pool(),
        access["num_os"],
    )


@router.get("/supervision-records/{work_order_number}/media")
async def get_record_media(
    work_order_number: str = Path(..., min_length=1, max_length=120),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> list[dict]:
    # La supervision es la fuente de verdad en Supabase: se valida acceso alli.
    # Las evidencias viven en la BD local, keyed por num_os (work_order_number).
    await get_supervision_record_access(
        get_supervision_pool(),
        work_order_number,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    await ensure_supervision_media_schema(get_pool())
    return await list_supervision_media_rows(
        get_pool(),
        work_order_number,
    )


def _parse_captured_at(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.post("/supervision-records/{work_order_number}/media")
async def post_record_media(
    work_order_number: str = Path(..., min_length=1, max_length=120),
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
    await ensure_supervision_media_schema(media_pool)

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

    if len(content) > 400 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo excede el limite permitido de 400 MB.")

    # Valida acceso contra Supabase (fuente de verdad de la supervision) antes de
    # tocar disco. work_order_number (num_os) es la clave estable que llega del cliente.
    access = await get_supervision_record_access(
        get_supervision_pool(),
        work_order_number,
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

    # El nombre de archivo usa el suministro (nis_rad) para que el equipo lo
    # identifique de un vistazo en la carpeta compartida; cae a work_order_number
    # si la supervision no tiene suministro asociado.
    supply_code = str(access.get("nis_rad") or work_order_number)

    target_dir = supervision_media_dir(work_order_number)

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
        media_path = supervision_media_url(work_order_number, file_name)
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


@router.api_route("/supervision-records/{record_id}/signed-pdf", methods=["GET", "HEAD"])
async def get_signed_supervision_pdf(
    record_id: int = Path(..., ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> FileResponse:
    access = await get_supervision_access(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    pdf_path = find_supervision_signed_pdf(
        access.get("num_os"),
        access.get("nis_rad"),
        record_id,
    )
    if pdf_path is None:
        raise HTTPException(status_code=404, detail="Aun no existe un PDF firmado para esta actividad.")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
    )


@router.post("/supervision-records/{record_id}/signed-pdf")
async def post_signed_supervision_pdf(
    record_id: int = Path(..., ge=1),
    file: UploadFile = File(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="El documento firmado debe ser un PDF.")

    access = await get_supervision_access(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El PDF firmado esta vacio.")
    if len(content) > MAX_SIGNED_PDF_BYTES:
        raise HTTPException(status_code=400, detail="El PDF firmado excede el limite de 15 MB.")

    try:
        pdf_path = supervision_signed_pdf_path(
            access.get("num_os"),
            access.get("nis_rad"),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    temporary_path = pdf_path.with_suffix(".tmp")
    temporary_path.write_bytes(content)
    temporary_path.replace(pdf_path)

    return {"saved": True, "size": len(content)}


@router.post("/supervision-records/{record_id}/photos")
async def post_record_photo(
    record_id: int = Path(..., ge=1),
    payload: SupervisionPhotoCreateRequest = Body(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    # Acceso contra Supabase (fuente de verdad); foto en BD local por num_os.
    access = await get_supervision_access(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    return await register_supervision_photo(
        get_pool(),
        access["num_os"],
        access.get("nis_rad"),
        payload.photoPath,
        payload.description,
    )


@router.delete("/supervision-records/{record_id}/photos")
async def delete_record_photo(
    record_id: int = Path(..., ge=1),
    photo_id: int = Query(..., alias="photoId", ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    # Acceso contra Supabase (fuente de verdad); borrado en BD local por id de foto.
    await get_supervision_access(
        get_supervision_pool(),
        record_id,
        user_role=user_role,
        user_id=parse_user_id(user_id),
    )
    return await delete_supervision_photo(
        get_pool(),
        photo_id,
    )


@router.post("/supervision-import")
async def post_import(
    payload: SupervisionImportRequest = Body(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    actor_user_id = parse_user_id(user_id)
    await ensure_path_access(
        get_pool(),
        user_id=actor_user_id,
        user_role=user_role,
        path="/supervisiones",
    )
    if is_admin(user_role):
        assigned_user_id = payload.assignedUserId
        if assigned_user_id is None:
            raise HTTPException(status_code=400, detail="Selecciona el usuario asignado para la importacion.")
        allow_reassignment = True
    else:
        if actor_user_id is None:
            raise HTTPException(status_code=401, detail="Tu usuario no tiene un perfil operativo vinculado.")
        # La asignacion movil siempre se deriva del Bearer validado por el middleware.
        # Nunca se confia en assignedUserId enviado por el cliente.
        assigned_user_id = actor_user_id
        allow_reassignment = False

    return await import_supervision_txt_files(
        get_supervision_pool(),
        [{"content": file.content, "fileName": file.fileName} for file in payload.files],
        assigned_user_id=assigned_user_id,
        allow_reassignment=allow_reassignment,
    )


class CreateSupervisionDraftRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


class MarkExportedRequest(BaseModel):
    supervisionIds: list[int] = Field(default_factory=list)


@router.post("/supervision-records")
async def post_create_draft(
    body: CreateSupervisionDraftRequest = Body(...),
    user_role: str | None = Header(default=None, alias="x-user-role"),
    user_id: str | None = Header(default=None, alias="x-user-id"),
) -> dict:
    actor_user_id = parse_user_id(user_id)
    if actor_user_id is None:
        raise HTTPException(status_code=401, detail="Tu usuario no tiene un perfil operativo vinculado.")
    return await create_supervision_draft(
        get_supervision_pool(),
        body.payload,
        user_id=actor_user_id,
    )


@router.delete("/supervision-records/{record_id}")
async def delete_record(
    record_id: int = Path(..., ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    if not is_admin(user_role):
        raise HTTPException(status_code=403, detail="Solo un administrador puede anular una supervision.")
    await soft_delete_supervision(
        get_supervision_pool(),
        record_id,
        deleted_by_user_id=None,
        deleted_by_name=None,
    )
    return {"success": True}


@router.post("/supervision-records/{record_id}/restore")
async def post_restore_record(
    record_id: int = Path(..., ge=1),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    if not is_admin(user_role):
        raise HTTPException(status_code=403, detail="Solo un administrador puede restaurar una supervision.")
    await restore_supervision(get_supervision_pool(), record_id)
    return {"success": True}


@router.post("/supervision-records/mark-exported")
async def post_mark_exported(
    body: MarkExportedRequest = Body(...),
) -> dict:
    await mark_supervisions_exported(get_supervision_pool(), body.supervisionIds)
    return {"success": True}


@router.get("/supervision-records/completed-range")
async def get_completed_range(
    start: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> list[dict]:
    if not is_admin(user_role):
        raise HTTPException(status_code=403, detail="Solo un administrador puede consultar este listado.")
    return await list_completed_supervisions_for_bulk_notice(get_supervision_pool(), start, end)
