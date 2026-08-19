from pathlib import Path

import httpx
from fastapi import APIRouter, Body, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_pool
from app.repositories.shared import fetch_all_dict
from app.supabase_auth import get_supabase_identity, verify_supabase_token


router = APIRouter(prefix="/v1/auth", tags=["sedapalgis-auth"])


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=1, max_length=4096)


def _read_public_web_env(name: str) -> str | None:
    # Compatibilidad de desarrollo. En servidores se configura
    # SUPABASE_ANON_KEY directamente en el entorno de FastAPI.
    path = Path(r"D:\Sedapal\apps\web\.env")
    if not path.is_file():
        return None
    prefix = f"{name}="
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith(prefix):
            return raw_line[len(prefix):].strip().strip('"').strip("'") or None
    return None


def _auth_config() -> tuple[str, str]:
    settings = get_settings()
    url = settings.supabase_url or _read_public_web_env("NEXT_PUBLIC_SUPABASE_URL")
    anon_key = settings.supabase_anon_key or _read_public_web_env("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    if not url or not anon_key:
        raise HTTPException(status_code=503, detail="Supabase Auth no esta configurado.")
    return url.rstrip("/"), anon_key


async def _resolve_email(identifier: str) -> str:
    normalized = identifier.strip().lower()
    if "@" in normalized:
        return normalized
    rows = await fetch_all_dict(
        get_pool(),
        """
        SELECT email
        FROM public.auth_profiles_local
        WHERE lower(username) = %s OR lower(email) = %s
        LIMIT 1
        """,
        [normalized, normalized],
    )
    if not rows or not rows[0].get("email"):
        raise HTTPException(status_code=401, detail="Usuario o contrasena incorrectos.")
    return str(rows[0]["email"])


async def _token_request(grant_type: str, payload: dict) -> dict:
    url, anon_key = _auth_config()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
        response = await client.post(
            f"{url}/auth/v1/token",
            params={"grant_type": grant_type},
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code in {400, 401}:
        raise HTTPException(status_code=401, detail="Usuario o contrasena incorrectos.")
    if not response.is_success:
        raise HTTPException(status_code=503, detail="Supabase Auth no esta disponible.")
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail="Supabase Auth devolvio una sesion invalida.")
    verify_supabase_token(token)
    user = data.get("user") or {}
    return {
        "accessToken": token,
        "refreshToken": data.get("refresh_token"),
        "expiresIn": data.get("expires_in", 3600),
        "tokenType": data.get("token_type", "bearer"),
        "user": {"id": user.get("id"), "email": user.get("email")},
    }


@router.post("/login")
async def login(body: LoginRequest) -> dict:
    return await _token_request(
        "password", {"email": await _resolve_email(body.identifier), "password": body.password}
    )


@router.post("/refresh")
async def refresh(body: RefreshRequest) -> dict:
    return await _token_request("refresh_token", {"refresh_token": body.refreshToken})


@router.get("/me")
async def me(authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
    identity = await get_supabase_identity(authorization)
    if not identity:
        raise HTTPException(status_code=401, detail="No autenticado.")
    profile = identity["profile"]
    return {
        "id": identity["auth_user_id"],
        "email": profile.get("email"),
        "role": identity["role"],
    }


@router.post("/logout")
async def logout(authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
    token = authorization.partition(" ")[2] if authorization else ""
    verify_supabase_token(token)
    url, anon_key = _auth_config()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
        await client.post(
            f"{url}/auth/v1/logout",
            headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
        )
    return {"success": True}
