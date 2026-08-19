import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.repositories.auth import get_access_snapshot, get_user_by_id


def _secret() -> bytes:
    settings = get_settings()
    return (settings.mobile_token_secret or settings.api_key).encode("utf-8")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _unbase64url(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("utf-8"))


def _sign(payload: str) -> str:
    return _base64url(hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest())


def create_mobile_token(*, user_id: int, role: str | None) -> tuple[str, str]:
    settings = get_settings()
    expires_at = int(time.time()) + settings.mobile_token_ttl_seconds
    payload = _base64url(
        json.dumps(
            {
                "exp": expires_at,
                "role": role,
                "sub": user_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    token = f"{payload}.{_sign(payload)}"
    iso_expires_at = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    return token, iso_expires_at


def verify_mobile_token(token: str | None) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Token movil ausente.")

    try:
        payload_part, signature = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Token movil invalido.") from None

    if not hmac.compare_digest(signature, _sign(payload_part)):
        raise HTTPException(status_code=401, detail="Token movil invalido.")

    try:
        payload = json.loads(_unbase64url(payload_part).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Token movil invalido.") from None

    if int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="Token movil expirado.")

    return payload


def read_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def get_mobile_context(pool: AsyncConnectionPool, authorization: str | None) -> dict | None:
    token = read_bearer_token(authorization)
    if not token:
        return None

    payload = verify_mobile_token(token)
    user_id = int(payload.get("sub") or 0)
    user = await get_user_by_id(pool, user_id)
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="Usuario movil inactivo o no encontrado.")

    access = await get_access_snapshot(pool, user_id=user_id, role=user.get("role"))
    return {
        "access": access,
        "assignment_code": user.get("assignment_code"),
        "role": user.get("role"),
        "user": user,
        "user_id": user_id,
    }


def to_session_user(user: dict) -> dict:
    return {
        "assignmentCode": user.get("assignment_code"),
        "email": user.get("email"),
        "fullName": user.get("full_name"),
        "id": user.get("id"),
        "isActive": user.get("is_active"),
        "role": user.get("role"),
        "username": user.get("username"),
    }
