"""Construccion de manifests y respuestas del protocolo Expo Updates (v1).

Todo el formato de bytes en este modulo esta verificado contra el codigo fuente
del cliente nativo instalado en el repo movil
(``node_modules/expo-updates/android/src/main/java/expo/modules/updates/``),
no solo contra la documentacion publica (que tiene huecos en varios puntos
criticos, como el formato exacto de fecha o el encoding de la firma):

- ``loader/FileDownloader.kt`` (``parseMultipartRemoteUpdateResponse``,
  ``createManifestRequest``): headers de request/response, framing multipart,
  bytes exactos que se firman (``body.readUtf8().toByteArray()``, UTF-8).
- ``codesigning/CodeSigningConfiguration.kt``: algoritmo ``SHA256withRSA``
  sobre esos bytes, firma en Base64 estandar (no url-safe) dentro del header
  ``expo-signature: sig="<base64>", keyid="main"``.
- ``UpdatesUtils.kt``: fechas ``yyyy-MM-dd'T'HH:mm:ss.SSS'Z'`` (milisegundos +
  'Z' literal) para ``createdAt``/``commitTime``; hash de asset en
  Base64 URL-safe SIN padding.
- ``loader/RemoteUpdate.kt``: forma exacta de las directivas
  ``noUpdateAvailable`` / ``rollBackToEmbedded`` (``{"type": ..., "parameters":
  {"commitTime": ...}}``, sin mas campos requeridos).

IMPORTANTE: el multipart se arma a mano con bytes crudos, nunca con
``email.mime.multipart`` u otra libreria que pueda reformatear el cuerpo — el
cliente firma/verifica sobre los bytes EXACTOS que recibe entre los headers de
la parte y el boundary siguiente.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import Response

from app.ota_signing import OtaSigningNotConfigured, sign_manifest_bytes
from app.ota_storage import ota_bundle_dir

CONTENT_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "json": "application/json",
    "wasm": "application/wasm",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
}

SUPPORTED_PLATFORMS = ("ios", "android")


def format_expo_timestamp(dt: datetime) -> str:
    """``yyyy-MM-dd'T'HH:mm:ss.SSS'Z'`` -- el unico formato que
    ``UpdatesUtils.parseDateString`` acepta en el cliente."""

    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"


def now_expo_timestamp() -> str:
    return format_expo_timestamp(datetime.now(timezone.utc))


def _base64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _build_asset_entry(
    *,
    content: bytes,
    relative_path: str,
    content_type: str,
    file_extension: str | None,
    channel: str,
    runtime_version: str,
    update_id: str,
    base_url: str,
) -> dict:
    digest = hashlib.sha256(content).digest()
    entry = {
        "key": hashlib.sha256(content).hexdigest(),
        "contentType": content_type,
        "hash": _base64url_nopad(digest),
        "url": f"{base_url}/api/updates/assets/{channel}/{runtime_version}/{update_id}/{relative_path}",
    }
    if file_extension:
        entry["fileExtension"] = file_extension
    return entry


def build_platform_manifests(
    *,
    metadata: dict,
    files_on_disk: dict[str, Path],
    update_id: str,
    runtime_version: str,
    channel: str,
    base_url: str,
) -> dict[str, dict]:
    """Un manifest JSON por plataforma presente en ``metadata.json`` (el
    output de ``expo export``)."""

    created_at = now_expo_timestamp()
    file_metadata = metadata.get("fileMetadata", {})
    manifests: dict[str, dict] = {}

    for platform in SUPPORTED_PLATFORMS:
        platform_meta = file_metadata.get(platform)
        if not platform_meta:
            continue

        bundle_rel_path = platform_meta["bundle"].replace("\\", "/")
        bundle_path = files_on_disk.get(bundle_rel_path)
        if bundle_path is None:
            raise HTTPException(
                status_code=400,
                detail=f"Falta el bundle declarado en metadata.json para {platform}: {bundle_rel_path}",
            )

        launch_asset = _build_asset_entry(
            content=bundle_path.read_bytes(),
            relative_path=bundle_rel_path,
            content_type="application/javascript",
            file_extension=".hbc",
            channel=channel,
            runtime_version=runtime_version,
            update_id=update_id,
            base_url=base_url,
        )

        assets = []
        for asset_meta in platform_meta.get("assets", []):
            rel_path = str(asset_meta["path"]).replace("\\", "/")
            ext = str(asset_meta.get("ext") or "").lstrip(".")
            asset_path = files_on_disk.get(rel_path)
            if asset_path is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Falta un asset declarado en metadata.json: {rel_path}",
                )
            assets.append(
                _build_asset_entry(
                    content=asset_path.read_bytes(),
                    relative_path=rel_path,
                    content_type=CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream"),
                    file_extension=f".{ext}" if ext else None,
                    channel=channel,
                    runtime_version=runtime_version,
                    update_id=update_id,
                    base_url=base_url,
                )
            )

        manifests[platform] = {
            "id": update_id,
            "createdAt": created_at,
            "runtimeVersion": runtime_version,
            "launchAsset": launch_asset,
            "assets": assets,
            "metadata": {},
            "extra": {},
        }

    if not manifests:
        raise HTTPException(
            status_code=400,
            detail="metadata.json no declara ningun bundle para ios/android.",
        )

    return manifests


def save_uploaded_files(
    *, channel: str, runtime_version: str, update_id: str, files: dict[str, bytes]
) -> tuple[dict[str, Path], Path]:
    """Guarda cada archivo recibido bajo su ruta relativa dentro del bundle,
    creando subcarpetas segun haga falta. Devuelve (mapa ruta -> Path, carpeta del bundle)."""

    bundle_dir = ota_bundle_dir(channel, runtime_version, update_id)
    saved: dict[str, Path] = {}
    for relative_path, content in files.items():
        normalized = relative_path.replace("\\", "/").lstrip("/")
        if ".." in normalized.split("/"):
            raise HTTPException(status_code=400, detail=f"Ruta de archivo invalida: {relative_path}")
        target_path = bundle_dir / normalized
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)
        saved[normalized] = target_path
    return saved, bundle_dir


def _multipart_response(part_name: str, body: bytes, extra_headers: dict[str, str]) -> Response:
    boundary = f"sedapal-ota-{uuid4().hex}"

    try:
        signature_value = sign_manifest_bytes(body)
        signature_line = f'expo-signature: {signature_value}\r\n'.encode("ascii")
    except OtaSigningNotConfigured:
        signature_line = b""

    part_headers = (
        f'--{boundary}\r\n'
        f'content-disposition: form-data; name="{part_name}"\r\n'
        f'content-type: application/json; charset=utf-8\r\n'
    ).encode("ascii")

    raw_body = part_headers + signature_line + b"\r\n" + body + f"\r\n--{boundary}--\r\n".encode("ascii")

    headers = {
        "expo-protocol-version": "1",
        "expo-sfv-version": "0",
        "cache-control": "private, no-cache",
        **extra_headers,
    }
    return Response(
        content=raw_body,
        media_type=f'multipart/mixed; boundary="{boundary}"',
        headers=headers,
    )


def manifest_response(manifest: dict, *, extra_headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    return _multipart_response("manifest", body, extra_headers or {})


def no_update_available_response(*, extra_headers: dict[str, str] | None = None) -> Response:
    body = json.dumps({"type": "noUpdateAvailable"}, separators=(",", ":")).encode("utf-8")
    return _multipart_response("directive", body, extra_headers or {})


def rollback_to_embedded_response(
    *, commit_time: str, extra_headers: dict[str, str] | None = None
) -> Response:
    directive = {
        "type": "rollBackToEmbedded",
        "parameters": {"commitTime": commit_time},
    }
    body = json.dumps(directive, separators=(",", ":")).encode("utf-8")
    return _multipart_response("directive", body, extra_headers or {})
