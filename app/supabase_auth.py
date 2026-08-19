import jwt
from fastapi import HTTPException
from jwt import PyJWKClient
from psycopg_pool import AsyncConnectionPool

from app.authz import normalize_assignment_code, normalize_role
from app.config import get_settings
from app.database import get_pool
from app.repositories.auth import get_access_snapshot
from app.repositories.shared import fetch_all_dict

_jwks_client: PyJWKClient | None = None


def _get_jwks_client(supabase_url: str) -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url)
    return _jwks_client


def read_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def verify_supabase_token(token: str | None) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Token de Supabase ausente.")

    settings = get_settings()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="JWT de Supabase invalido.") from None

    alg = header.get("alg")
    audience = settings.supabase_jwt_audience
    decode_options = {"verify_aud": bool(audience)}

    try:
        if alg == "HS256":
            secret = settings.supabase_jwt_secret
            if not secret:
                raise HTTPException(status_code=500, detail="Falta SUPABASE_JWT_SECRET en FastAPI.")
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=audience,
                options=decode_options,
            )
        elif alg in ("ES256", "RS256"):
            if not settings.supabase_url:
                raise HTTPException(status_code=500, detail="Falta SUPABASE_URL en FastAPI.")
            jwk_client = _get_jwks_client(settings.supabase_url)
            signing_key = jwk_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience=audience,
                options=decode_options,
            )
        else:
            raise HTTPException(status_code=401, detail="Algoritmo JWT no soportado.")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="JWT de Supabase expirado.") from None
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Audience JWT invalida.") from None
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="JWT de Supabase invalido.") from None

    issuer = settings.supabase_jwt_issuer
    if issuer and payload.get("iss") != issuer:
        raise HTTPException(status_code=401, detail="Issuer JWT invalido.")

    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="JWT de Supabase sin subject.")

    return payload


async def get_supabase_identity(authorization: str | None) -> dict | None:
    token = read_bearer_token(authorization)
    if not token:
        return None

    payload = verify_supabase_token(token)
    auth_user_id = str(payload.get("sub") or "").strip()

    # Supabase entrega la identidad; roles, perfil y permisos se resuelven
    # exclusivamente en PostgreSQL local mediante auth_user_id.
    rows = await fetch_all_dict(
        get_pool(),
        """
        SELECT
            p.id::text AS auth_user_id,
            p.full_name,
            p.role::text AS role,
            COALESCE(a.assignment_code, p.employee_code) AS employee_code,
            a.email,
            a.username,
            a.legacy_user_id,
            p.active
        FROM public.profiles p
        LEFT JOIN public.auth_profiles_local a ON a.auth_user_id = p.id
        WHERE p.id = %s::uuid
        LIMIT 1;
        """,
        [auth_user_id],
    )

    if not rows:
        raise HTTPException(status_code=401, detail="Perfil local no encontrado.")

    profile = rows[0]
    if profile.get("active") is False:
        raise HTTPException(status_code=403, detail="Usuario inactivo.")

    legacy_user_id = profile.get("legacy_user_id")

    return {
        "assignment_code": normalize_assignment_code(profile.get("employee_code")),
        "auth_user_id": auth_user_id,
        "legacy_user_id": int(legacy_user_id) if legacy_user_id is not None else None,
        "profile": profile,
        "role": normalize_role(profile.get("role")),
    }


async def get_supabase_context(pool: AsyncConnectionPool, authorization: str | None) -> dict | None:
    identity = await get_supabase_identity(authorization)
    if not identity:
        return None

    legacy_user_id = identity.get("legacy_user_id")
    role = identity.get("role")
    access = (
        await get_access_snapshot(pool, user_id=legacy_user_id, role=role)
        if legacy_user_id is not None
        else {"allowedApiPrefixes": [], "allowedPaths": [], "homePath": "/login", "menuGroups": [], "views": []}
    )

    return {
        "access": access,
        "assignment_code": identity.get("assignment_code"),
        "auth_user_id": identity.get("auth_user_id"),
        "legacy_user_id": legacy_user_id,
        "role": role,
        "user": identity.get("profile"),
    }


def to_session_user(profile: dict) -> dict:
    return {
        "assignmentCode": normalize_assignment_code(profile.get("employee_code")),
        "email": profile.get("email"),
        "fullName": profile.get("full_name"),
        "id": 0,
        "isActive": profile.get("active", True),
        "role": normalize_role(profile.get("role")),
        "username": profile.get("username") or profile.get("full_name"),
    }
