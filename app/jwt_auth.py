"""Auth propio (Fase 2 de la migracion AWS): JWT de acceso de corta duracion +
refresh token opaco de larga duracion, para reemplazar Supabase Auth sin
depender de ningun servicio externo. Convive con `app/supabase_auth.py`
durante la transicion -- `resolve_actor` en ese modulo intenta verificar el
JWT propio primero y solo cae a Supabase si no es un token propio, asi que
todos los endpoints que ya usan `resolve_actor` (toda la Fase 3) aceptan
ambos sin cambios adicionales.

Diseño del refresh token: nunca se guarda el valor crudo en BD (si la BD se
filtra, los tokens no sirven de nada sin poder invertir el hash). Cada uso
rota el token (uno nuevo reemplaza al usado) y detecta reuso: si alguien
presenta un refresh token ya usado/revocado, se interpreta como robo y se
revocan TODOS los tokens activos de ese usuario.
"""

import hashlib
import secrets
import time
from datetime import datetime, timezone

import jwt
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.repositories.auth import get_user_by_id

_refresh_tokens_schema_ready = False


def _jwt_secret() -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="Falta JWT_SECRET en la configuracion del servidor.")
    return settings.jwt_secret


def create_access_token(*, user_id: int, role: str | None) -> tuple[str, int]:
    settings = get_settings()
    now = int(time.time())
    expires_at = now + settings.jwt_access_ttl_seconds
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iss": "sedapal-native",
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token, expires_at


def verify_access_token(token: str | None) -> dict | None:
    """Devuelve el payload si `token` es un JWT propio valido y vigente, o
    `None` en cualquier otro caso (firma invalida, expirado, no es un JWT
    propio -- p.ej. es un JWT de Supabase). Nunca lanza: es el punto de
    entrada que `resolve_actor` usa para decidir, sin excepciones, si debe
    intentar la verificacion de Supabase como alternativa."""
    if not token:
        return None
    settings = get_settings()
    if not settings.jwt_secret:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], issuer="sedapal-native")
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


def _hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def ensure_refresh_tokens_schema(pool: AsyncConnectionPool) -> None:
    global _refresh_tokens_schema_ready
    if _refresh_tokens_schema_ready:
        return
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.refresh_tokens (
                  id bigserial PRIMARY KEY,
                  user_id bigint NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                  token_hash text NOT NULL UNIQUE,
                  created_at timestamptz NOT NULL DEFAULT NOW(),
                  expires_at timestamptz NOT NULL,
                  revoked_at timestamptz,
                  replaced_by_id bigint REFERENCES public.refresh_tokens(id) ON DELETE SET NULL,
                  user_agent text,
                  ip text
                );
                """
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON public.refresh_tokens(user_id);"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token_hash ON public.refresh_tokens(token_hash);"
            )
        await connection.commit()
    _refresh_tokens_schema_ready = True


async def issue_refresh_token(
    pool: AsyncConnectionPool,
    *,
    user_id: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    await ensure_refresh_tokens_schema(pool)
    settings = get_settings()
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_token)

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO public.refresh_tokens (user_id, token_hash, expires_at, user_agent, ip)
                VALUES (%s, %s, NOW() + make_interval(secs => %s), %s, %s);
                """,
                (user_id, token_hash, settings.jwt_refresh_ttl_seconds, user_agent, ip),
            )
        await connection.commit()
    return raw_token


async def revoke_all_user_tokens(pool: AsyncConnectionPool, user_id: int) -> None:
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.refresh_tokens
                SET revoked_at = NOW()
                WHERE user_id = %s AND revoked_at IS NULL;
                """,
                (user_id,),
            )
        await connection.commit()


async def revoke_refresh_token(pool: AsyncConnectionPool, raw_token: str) -> None:
    await ensure_refresh_tokens_schema(pool)
    token_hash = _hash_refresh_token(raw_token)
    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE public.refresh_tokens
                SET revoked_at = NOW()
                WHERE token_hash = %s AND revoked_at IS NULL;
                """,
                (token_hash,),
            )
        await connection.commit()


async def rotate_refresh_token(
    pool: AsyncConnectionPool,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> dict:
    """Valida `raw_token`, lo marca usado y emite un par nuevo (access +
    refresh). Si el token ya estaba revocado (reuso -- indicio de robo),
    revoca TODOS los tokens del usuario y rechaza la solicitud."""
    await ensure_refresh_tokens_schema(pool)
    token_hash = _hash_refresh_token(raw_token)

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT id, user_id, expires_at, revoked_at
                FROM public.refresh_tokens
                WHERE token_hash = %s
                LIMIT 1;
                """,
                (token_hash,),
            )
            row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="Refresh token invalido.")

    if row["revoked_at"] is not None:
        # Reuso de un token ya rotado/revocado: tratamos como robo y cerramos
        # todas las sesiones activas de este usuario.
        await revoke_all_user_tokens(pool, row["user_id"])
        raise HTTPException(status_code=401, detail="Sesion invalidada por seguridad. Inicia sesion nuevamente.")

    from datetime import datetime, timezone

    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token expirado.")

    from app.repositories.auth import get_user_by_id

    user = await get_user_by_id(pool, row["user_id"])
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="Usuario inactivo o no encontrado.")

    new_raw_token = secrets.token_urlsafe(48)
    new_token_hash = _hash_refresh_token(new_raw_token)
    settings = get_settings()

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO public.refresh_tokens (user_id, token_hash, expires_at, user_agent, ip)
                VALUES (%s, %s, NOW() + make_interval(secs => %s), %s, %s)
                RETURNING id;
                """,
                (row["user_id"], new_token_hash, settings.jwt_refresh_ttl_seconds, user_agent, ip),
            )
            new_row = await cursor.fetchone()
            await cursor.execute(
                """
                UPDATE public.refresh_tokens
                SET revoked_at = NOW(), replaced_by_id = %s
                WHERE id = %s;
                """,
                (new_row["id"], row["id"]),
            )
        await connection.commit()

    access_token, access_expires_at = create_access_token(user_id=user["id"], role=user.get("role"))
    return {
        "accessToken": access_token,
        "accessTokenExpiresAt": access_expires_at,
        "refreshToken": new_raw_token,
        "user": user,
    }
