"""Servidor propio de actualizaciones OTA, compatible con el protocolo Expo
Updates v1 (reemplaza a Expo EAS Update / u.expo.dev).

``GET /manifest`` y ``GET /assets/...`` quedan PUBLICOS a proposito (sin
x-api-key ni JWT): el modulo nativo ``expo-updates`` no puede aportar
credenciales dinamicas (el chequeo de updates ocurre antes de que exista
sesion JS), asi que autenticar por HTTP no aporta seguridad real -- la
integridad la da el code signing (``app/ota_signing.py``), igual que el
propio servicio ``u.expo.dev`` de Expo tambien es publico. NO agregar
x-api-key/JWT a estas dos rutas sin entender esto primero: dejaria a los
dispositivos en campo sin poder recibir actualizaciones.

``POST /publish``, ``/rollback`` y ``/activate`` si quedan protegidos (via el
middleware global de ``app/main.py``, que exige x-api-key o JWT para todo lo
que no este en su lista de rutas publicas).
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_pool
from app.ota_signing import OtaSigningNotConfigured, sign_manifest_bytes
from app.ota_storage import ota_bundle_file
from app.repositories.ota_updates import (
    activate_update,
    get_latest_rolled_back,
    get_published_update,
    insert_published_update,
    mark_rolled_back,
)
from app.services.push_notifications import notify_ota_update_available
from app.services.ota_updates import (
    CONTENT_TYPE_BY_EXT,
    SUPPORTED_PLATFORMS,
    build_platform_manifests,
    format_expo_timestamp,
    manifest_response,
    no_update_available_response,
    rollback_to_embedded_response,
    save_uploaded_files,
)

router = APIRouter(prefix="/updates", tags=["updates"])

DEFAULT_CHANNEL = "production"


@router.get("/manifest")
async def get_manifest(
    expo_platform: str | None = Header(default=None, alias="expo-platform"),
    expo_runtime_version: str | None = Header(default=None, alias="expo-runtime-version"),
    expo_protocol_version: str | None = Header(default=None, alias="expo-protocol-version"),
    expo_channel_name: str | None = Header(default=None, alias="expo-channel-name"),
    expo_current_update_id: str | None = Header(default=None, alias="expo-current-update-id"),
):
    if expo_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail="Header expo-platform invalido o ausente.")
    if not expo_runtime_version:
        raise HTTPException(status_code=400, detail="Header expo-runtime-version ausente.")

    channel = (expo_channel_name or DEFAULT_CHANNEL).strip()
    if channel != DEFAULT_CHANNEL:
        # v1: un solo canal soportado. Development/preview se prueban local,
        # no via este servidor (decision confirmada con el usuario).
        raise HTTPException(
            status_code=400, detail=f"Canal '{channel}' no soportado; solo '{DEFAULT_CHANNEL}'."
        )

    try:
        protocol_version = int(expo_protocol_version) if expo_protocol_version else 0
    except ValueError:
        protocol_version = 0

    pool = get_pool()
    published = await get_published_update(
        pool, channel=channel, platform=expo_platform, runtime_version=expo_runtime_version
    )

    current_id_normalized = (expo_current_update_id or "").strip().lower()

    if published and str(published["id"]).lower() == current_id_normalized:
        return _no_update_response(protocol_version)

    if published:
        return _manifest_response_for_protocol(published["manifest_json"], protocol_version)

    rolled_back = await get_latest_rolled_back(
        pool, channel=channel, platform=expo_platform, runtime_version=expo_runtime_version
    )
    if rolled_back and protocol_version >= 1:
        commit_time = format_expo_timestamp(rolled_back["rolled_back_at"])
        return rollback_to_embedded_response(commit_time=commit_time)

    return _no_update_response(protocol_version)


def _no_update_response(protocol_version: int) -> Response:
    if protocol_version >= 1:
        return no_update_available_response()
    # v0 no tiene directivas: un 204 sin cuerpo significa "sin cambios".
    return Response(status_code=204)


def _manifest_response_for_protocol(manifest: dict, protocol_version: int) -> Response:
    if protocol_version >= 1:
        return manifest_response(manifest)

    # Fallback v0: JSON plano, firma como header de respuesta (no hay partes
    # multipart en este modo).
    body = json.dumps(manifest).encode("utf-8")
    headers = {"expo-protocol-version": "0"}
    try:
        headers["expo-signature"] = sign_manifest_bytes(body)
    except OtaSigningNotConfigured:
        pass
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/assets/{channel}/{runtime_version}/{update_id}/{asset_path:path}")
async def get_asset(channel: str, runtime_version: str, update_id: str, asset_path: str):
    try:
        file_path = ota_bundle_file(channel, runtime_version, update_id, asset_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Asset no encontrado.")

    ext = file_path.suffix.lstrip(".").lower()
    content_type = "application/javascript" if ext == "hbc" else CONTENT_TYPE_BY_EXT.get(
        ext, "application/octet-stream"
    )

    return FileResponse(
        path=file_path,
        media_type=content_type,
        headers={"cache-control": "public, max-age=31536000, immutable"},
    )


@router.post("/publish", status_code=status.HTTP_201_CREATED)
async def publish_update(
    runtime_version: str = Form(...),
    channel: str = Form(default=DEFAULT_CHANNEL),
    commit_message: str | None = Form(default=None),
    commit_sha: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
) -> dict:
    settings = get_settings()
    pool = get_pool()

    file_bytes: dict[str, bytes] = {}
    for upload in files:
        if not upload.filename:
            continue
        file_bytes[upload.filename] = await upload.read()

    if "metadata.json" not in file_bytes:
        raise HTTPException(
            status_code=400,
            detail="Falta metadata.json entre los archivos subidos (salida de 'expo export').",
        )

    update_id = str(uuid4())
    saved_files, bundle_dir = save_uploaded_files(
        channel=channel, runtime_version=runtime_version, update_id=update_id, files=file_bytes
    )

    metadata = json.loads(saved_files["metadata.json"].read_bytes())
    manifests = build_platform_manifests(
        metadata=metadata,
        files_on_disk=saved_files,
        update_id=update_id,
        runtime_version=runtime_version,
        channel=channel,
        base_url=settings.ota_public_base_url,
    )

    for platform, manifest in manifests.items():
        await insert_published_update(
            pool,
            update_id=update_id,
            runtime_version=runtime_version,
            platform=platform,
            channel=channel,
            manifest_json=manifest,
            bundle_dir=str(bundle_dir),
            commit_message=commit_message,
            commit_sha=commit_sha,
        )

    try:
        await notify_ota_update_available(get_pool(), runtime_version=runtime_version)
    except RuntimeError:
        pass  # SUPABASE_DATABASE_URL no configurado en este entorno.

    return {
        "success": True,
        "updateId": update_id,
        "runtimeVersion": runtime_version,
        "channel": channel,
        "platforms": list(manifests.keys()),
    }


class RollbackRequest(BaseModel):
    runtime_version: str
    channel: str = DEFAULT_CHANNEL
    platform: str | None = None


@router.post("/rollback")
async def rollback_update(payload: RollbackRequest) -> dict:
    pool = get_pool()
    platforms = [payload.platform] if payload.platform else list(SUPPORTED_PLATFORMS)
    results = {
        platform: await mark_rolled_back(
            pool, channel=payload.channel, platform=platform, runtime_version=payload.runtime_version
        )
        for platform in platforms
    }
    return {"success": True, "rolledBack": results}


class ActivateRequest(BaseModel):
    update_id: str
    runtime_version: str
    channel: str = DEFAULT_CHANNEL
    platform: str | None = None


@router.post("/activate")
async def activate_update_route(payload: ActivateRequest) -> dict:
    pool = get_pool()
    platforms = [payload.platform] if payload.platform else list(SUPPORTED_PLATFORMS)
    results = {
        platform: await activate_update(
            pool,
            update_id=payload.update_id,
            channel=payload.channel,
            platform=platform,
            runtime_version=payload.runtime_version,
        )
        for platform in platforms
    }
    if not any(results.values()):
        raise HTTPException(status_code=404, detail="No se encontro ese update_id para activar.")
    return {"success": True, "activated": results}
