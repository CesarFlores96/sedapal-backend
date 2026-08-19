import base64
import hashlib
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException, Path as ApiPath, Query
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_pool
from app.repositories.shared import execute_fetch_all_dict, fetch_all_dict


router = APIRouter(prefix="/private", tags=["private-files"])


class FilePayload(BaseModel):
    contentBase64: str = Field(min_length=1, max_length=20_000_000)
    contentType: str = Field(min_length=1, max_length=160)
    fileName: str = Field(min_length=1, max_length=255)


class CertificatePayload(FilePayload):
    certIssuer: str = Field(max_length=1000)
    certSubject: str = Field(max_length=1000)
    certValidTo: str | None = None


class ManualSignaturePayload(FilePayload):
    extension: str = Field(pattern="^[a-zA-Z0-9]{2,5}$")


class GraphicSignaturePayload(BaseModel):
    workOrderNumber: str = Field(min_length=1, max_length=120)
    signerRole: str = Field(pattern="^(cliente|inspector|supervision)$")
    imageDataUrl: str = Field(min_length=20, max_length=20_000_000)


class SignaturePayload(BaseModel):
    certIssuer: str | None = None
    certSubject: str | None = None
    certValidTo: str | None = None
    signatureKind: str = Field(pattern="^(grafica|digital)$")
    signedByUserId: str
    signerName: str | None = None
    signerRole: str = Field(pattern="^(cliente|inspector|supervision)$")
    storagePath: str
    supervisionId: int = Field(ge=1)


def _authorize_profile(profile_id: str, auth_user_id: str | None, user_role: str | None) -> None:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    if auth_user_id != profile_id and (user_role or "").lower() not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Sin permiso para acceder al archivo.")


def _safe_path(bucket: str, object_path: str) -> tuple[Path, str]:
    clean_bucket = bucket.strip().replace("\\", "/").strip("/")
    clean_object = object_path.strip().replace("\\", "/").strip("/")
    if not clean_bucket or not clean_object or ".." in clean_bucket.split("/") or ".." in clean_object.split("/"):
        raise HTTPException(status_code=400, detail="Ruta de archivo invalida.")
    root = Path(get_settings().private_files_root).resolve()
    target = (root / clean_bucket / clean_object).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=400, detail="Ruta de archivo invalida.")
    return target, f"{clean_bucket}/{clean_object}"


async def _save_file(bucket: str, object_path: str, content: bytes, content_type: str) -> str:
    target, relative = _safe_path(bucket, object_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    await execute_fetch_all_dict(get_pool(), """
      INSERT INTO public.private_file_objects
        (bucket, object_path, local_relative_path, content_type, size_bytes, sha256, source_updated_at, migrated_at)
      VALUES (%s, %s, %s, %s, %s, %s, now(), now())
      ON CONFLICT (bucket, object_path) DO UPDATE SET local_relative_path = EXCLUDED.local_relative_path,
        content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes,
        sha256 = EXCLUDED.sha256, source_updated_at = now(), migrated_at = now()
      RETURNING object_path
    """, [bucket, object_path, relative, content_type, len(content), hashlib.sha256(content).hexdigest()])
    return object_path


async def _delete_file(bucket: str, object_path: str) -> None:
    target, _ = _safe_path(bucket, object_path)
    if target.is_file():
        target.unlink()
    await execute_fetch_all_dict(get_pool(), """
      DELETE FROM public.private_file_objects WHERE bucket = %s AND object_path = %s RETURNING object_path
    """, [bucket, object_path])


async def _read_file(bucket: str, object_path: str) -> tuple[bytes, str]:
    rows = await fetch_all_dict(get_pool(), """
      SELECT local_relative_path, content_type FROM public.private_file_objects
      WHERE bucket = %s AND object_path = %s LIMIT 1
    """, [bucket, object_path])
    if not rows:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    root = Path(get_settings().private_files_root).resolve()
    target = (root / rows[0]["local_relative_path"]).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=400, detail="Ruta de archivo invalida.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco.")
    content_type = rows[0].get("content_type") or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return target.read_bytes(), content_type


@router.get("/profiles/{profile_id}/certificate")
async def get_certificate(profile_id: str, auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                          user_role: str | None = Header(default=None, alias="x-user-role")) -> dict | None:
    _authorize_profile(profile_id, auth_user_id, user_role)
    rows = await fetch_all_dict(get_pool(), """
      SELECT profile_id::text, storage_path, file_name, cert_issuer, cert_subject,
        cert_valid_to::text, updated_at::text FROM public.profile_signature_certificates
      WHERE profile_id = %s::uuid LIMIT 1
    """, [profile_id])
    return rows[0] if rows else None


@router.put("/profiles/{profile_id}/certificate")
async def put_certificate(profile_id: str, payload: CertificatePayload = Body(...),
                          auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                          user_role: str | None = Header(default=None, alias="x-user-role")) -> dict:
    _authorize_profile(profile_id, auth_user_id, user_role)
    object_path = f"{profile_id}/certificate.p12"
    await _save_file("profile-signature-vault", object_path, base64.b64decode(payload.contentBase64), payload.contentType)
    rows = await execute_fetch_all_dict(get_pool(), """
      INSERT INTO public.profile_signature_certificates
        (profile_id, storage_path, file_name, cert_issuer, cert_subject, cert_valid_to, updated_at)
      VALUES (%s::uuid, %s, %s, %s, %s, %s, now())
      ON CONFLICT (profile_id) DO UPDATE SET storage_path = EXCLUDED.storage_path,
        file_name = EXCLUDED.file_name, cert_issuer = EXCLUDED.cert_issuer,
        cert_subject = EXCLUDED.cert_subject, cert_valid_to = EXCLUDED.cert_valid_to, updated_at = now()
      RETURNING profile_id::text, storage_path, file_name, cert_issuer, cert_subject,
        cert_valid_to::text, updated_at::text
    """, [profile_id, object_path, payload.fileName, payload.certIssuer, payload.certSubject, payload.certValidTo])
    return rows[0]


@router.delete("/profiles/{profile_id}/certificate")
async def delete_certificate(profile_id: str, auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                             user_role: str | None = Header(default=None, alias="x-user-role")) -> dict:
    _authorize_profile(profile_id, auth_user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), "DELETE FROM public.profile_signature_certificates WHERE profile_id = %s::uuid RETURNING storage_path", [profile_id])
    if rows:
        await _delete_file("profile-signature-vault", rows[0]["storage_path"])
    return {"success": True}


@router.get("/profiles/{profile_id}/manual-signature")
async def get_manual_signature(profile_id: str, include_content: bool = Query(default=False),
                               auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                               user_role: str | None = Header(default=None, alias="x-user-role")) -> dict | None:
    _authorize_profile(profile_id, auth_user_id, user_role)
    rows = await fetch_all_dict(get_pool(), """
      SELECT profile_id::text, storage_path, file_name, updated_at::text
      FROM public.profile_manual_signatures WHERE profile_id = %s::uuid LIMIT 1
    """, [profile_id])
    if not rows:
        return None
    if include_content:
        content, content_type = await _read_file("profile-signature-vault", rows[0]["storage_path"])
        rows[0]["dataUrl"] = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
    return rows[0]


@router.put("/profiles/{profile_id}/manual-signature")
async def put_manual_signature(profile_id: str, payload: ManualSignaturePayload = Body(...),
                               auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                               user_role: str | None = Header(default=None, alias="x-user-role")) -> dict:
    _authorize_profile(profile_id, auth_user_id, user_role)
    previous = await fetch_all_dict(get_pool(), "SELECT storage_path FROM public.profile_manual_signatures WHERE profile_id = %s::uuid", [profile_id])
    object_path = f"{profile_id}/manual-signature.{payload.extension.lower()}"
    if previous and previous[0]["storage_path"] != object_path:
        await _delete_file("profile-signature-vault", previous[0]["storage_path"])
    await _save_file("profile-signature-vault", object_path, base64.b64decode(payload.contentBase64), payload.contentType)
    rows = await execute_fetch_all_dict(get_pool(), """
      INSERT INTO public.profile_manual_signatures (profile_id, storage_path, file_name, updated_at)
      VALUES (%s::uuid, %s, %s, now()) ON CONFLICT (profile_id) DO UPDATE SET
        storage_path = EXCLUDED.storage_path, file_name = EXCLUDED.file_name, updated_at = now()
      RETURNING profile_id::text, storage_path, file_name, updated_at::text
    """, [profile_id, object_path, payload.fileName])
    return rows[0]


@router.delete("/profiles/{profile_id}/manual-signature")
async def delete_manual_signature(profile_id: str, auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                                  user_role: str | None = Header(default=None, alias="x-user-role")) -> dict:
    _authorize_profile(profile_id, auth_user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), "DELETE FROM public.profile_manual_signatures WHERE profile_id = %s::uuid RETURNING storage_path", [profile_id])
    if rows:
        await _delete_file("profile-signature-vault", rows[0]["storage_path"])
    return {"success": True}


@router.get("/files/content")
async def get_file_content(bucket: str = Query(...), object_path: str = Query(...),
                           auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
                           user_role: str | None = Header(default=None, alias="x-user-role")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    if bucket == "profile-signature-vault":
        owner_id = object_path.replace("\\", "/").split("/", 1)[0]
        _authorize_profile(owner_id, auth_user_id, user_role)
    content, content_type = await _read_file(bucket, object_path)
    return {"contentBase64": base64.b64encode(content).decode("ascii"), "contentType": content_type}


@router.post("/supervision-signatures/image")
async def post_signature_image(payload: GraphicSignaturePayload = Body(...),
                               auth_user_id: str | None = Header(default=None, alias="x-auth-user-id")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    header, encoded = payload.imageDataUrl.split(",", 1)
    content_type = header.removeprefix("data:").split(";", 1)[0]
    extension = "jpg" if content_type in {"image/jpg", "image/jpeg"} else "png"
    object_path = f"{payload.workOrderNumber}/{payload.signerRole}-{hashlib.sha256(encoded.encode()).hexdigest()[:16]}.{extension}"
    await _save_file("supervision-signatures", object_path, base64.b64decode(encoded), content_type)
    return {"storagePath": object_path}


@router.post("/supervision-signatures")
async def post_signature(payload: SignaturePayload = Body(...),
                         auth_user_id: str | None = Header(default=None, alias="x-auth-user-id")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")

    params = [payload.supervisionId, payload.signerRole, payload.signatureKind, payload.signerName,
              payload.certSubject, payload.certIssuer, payload.certValidTo, payload.storagePath, payload.signedByUserId]
    insert_query = """
        INSERT INTO public.supervision_signatures
          (supervision_id, signer_role, signature_kind, signer_name, cert_subject,
           cert_issuer, cert_valid_to, storage_path, signed_by_user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid) RETURNING id
    """

    try:
        rows = await execute_fetch_all_dict(get_pool(), insert_query, params)
    except UniqueViolation:
        # La migracion historica copio IDs explicitos desde Supabase y podia
        # dejar la secuencia local por debajo de MAX(id). Se realinea y se
        # reintenta una sola vez; no se ocultan otras restricciones de datos.
        await execute_fetch_all_dict(get_pool(), """
          SELECT setval(
            pg_get_serial_sequence('public.supervision_signatures', 'id'),
            COALESCE((SELECT max(id) FROM public.supervision_signatures), 1),
            EXISTS (SELECT 1 FROM public.supervision_signatures)
          ) AS sequence_value
        """)
        rows = await execute_fetch_all_dict(get_pool(), insert_query, params)
    return {"success": bool(rows)}


@router.get("/supervision-signatures/{supervision_id}")
async def get_signatures(supervision_id: int = ApiPath(..., ge=1),
                         auth_user_id: str | None = Header(default=None, alias="x-auth-user-id")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    rows = await fetch_all_dict(get_pool(), """
      SELECT id, supervision_id, signed_by_user_id::text, signer_role, signature_kind,
        signer_name, storage_path, cert_issuer, cert_subject, cert_valid_to::text, signed_at::text
      FROM public.supervision_signatures WHERE supervision_id = %s ORDER BY signed_at DESC
    """, [supervision_id])
    latest: dict[str, dict] = {}
    for row in rows:
        latest.setdefault(row["signer_role"], row)
    return {"hasSignatures": bool(rows), "latest": latest}
