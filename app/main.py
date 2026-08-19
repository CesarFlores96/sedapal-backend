from collections.abc import Awaitable, Callable
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import rate_limit
from app.config import get_settings
from app.database import (
    close_pool,
    close_specialized_pools,
    close_supabase_pool,
    get_gis_pool,
    open_pool,
    open_specialized_pools,
    open_supabase_pool,
)
from app.sedapalgis.repositories.gis import sync_supply_locations
from app.routers import (
    auth,
    analytics,
    billing,
    catalogs,
    chat,
    data_sources,
    customer_supplies,
    customers,
    dashboard,
    documents,  # NUEVO
    credit_notes,
    facturacion,
    geocoding,
    gis,
    health,
    inspections,
    meters,
    local_services,
    network,
    navigation,
    readings,
    reports,
    sedapalgis,
    sedapalgis_auth,
    supervisions,
    supervision_raw,
    supervision_table,
    updates,
    planillas,
    private_files,
)
from app.instance_lock import acquire_instance_lock, release_instance_lock
from app.supabase_auth import get_supabase_identity, read_bearer_token, verify_supabase_token


settings = get_settings()
UPLOADS_ROOT = Path(__file__).resolve().parent / "uploads"


@asynccontextmanager
async def lifespan(application: FastAPI):
    acquire_instance_lock()
    await open_pool()
    await open_specialized_pools()
    await open_supabase_pool()
    sync_task = asyncio.create_task(_keep_supply_locations_synced())
    try:
        yield
    finally:
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task
        await close_supabase_pool()
        await close_specialized_pools()
        await close_pool()
        release_instance_lock()


async def _keep_supply_locations_synced() -> None:
    while True:
        try:
            await sync_supply_locations(get_gis_pool())
        except Exception as exc:
            print(f"[WARN] No se pudo sincronizar ubicaciones GIS: {exc}")
        await asyncio.sleep(60)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_origin_regex=settings.allowed_origin_regex_value,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Authorization",
        "Content-Type",
        "Origin",
        "X-Requested-With",
        "x-api-key",
    ],
)


# Rutas exentas de rate limiting: salud y documentación (se consultan seguido y
# no cargan datos sensibles). El login sí se limita, con el nivel estricto.
RATE_LIMIT_SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}
AUTH_RATE_LIMIT_PATHS = {
    "/api/auth/mobile/login",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
}

# El manifest y los assets de OTA quedan publicos a proposito (el cliente
# nativo expo-updates no puede aportar x-api-key/JWT dinamico): la seguridad
# la da la firma de codigo, no la autenticacion HTTP. Ver docstring de
# app/routers/updates.py antes de "arreglar" esto agregando auth aqui.
PUBLIC_PATH_PREFIXES = (
    "/api/updates/assets/",
    "/api/v1/gis/tiles/",
    "/updater/",
    "/api/billing/credit-notes/",
)
PUBLIC_PATHS_EXTRA = {"/api/updates/manifest"}


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS_EXTRA:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def _client_ip(request: Request) -> str:
    """IP del cliente respetando un proxy inverso si lo hubiera."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _resolve_rate_limit(request: Request) -> rate_limit.RateLimitResult | None:
    """Elige el nivel según las credenciales y aplica la ventana correspondiente."""
    path = request.url.path
    ip = _client_ip(request)

    # MapLibre solicita varias teselas en paralelo. Aunque la ruta es pública,
    # cada URL lleva una sesión firmada y de corta duración; aplicarle el techo
    # anónimo de 30/minuto provoca 429 durante un paneo normal.
    if path.startswith("/api/v1/gis/tiles/"):
        return rate_limit.check(
            f"tiles:{ip}", settings.rate_limit_trusted_max, settings.rate_limit_trusted_window
        )

    if path in AUTH_RATE_LIMIT_PATHS:
        return rate_limit.check(
            f"auth:{ip}", settings.rate_limit_auth_max, settings.rate_limit_auth_window
        )

    provided_key = request.headers.get("x-api-key")
    if provided_key and provided_key == settings.api_key:
        return rate_limit.check(
            f"trusted:{ip}", settings.rate_limit_trusted_max, settings.rate_limit_trusted_window
        )

    bearer_token = read_bearer_token(request.headers.get("authorization"))
    if bearer_token:
        return rate_limit.check(
            f"user:{rate_limit.hash_token(bearer_token)}",
            settings.rate_limit_user_max,
            settings.rate_limit_user_window,
        )

    return rate_limit.check(
        f"anon:{ip}", settings.rate_limit_anon_max, settings.rate_limit_anon_window
    )


def _too_many_requests(result: rate_limit.RateLimitResult) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "detail": "Demasiadas solicitudes. Intenta nuevamente en unos segundos.",
        },
        headers={
            "Retry-After": str(result.retry_after),
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(result.reset),
        },
    )


@app.middleware("http")
async def api_key_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    public_paths = {
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/auth/mobile/login",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        # Auth propio (Fase 2): login/refresh/logout son el punto de entrada,
        # no pueden exigir un bearer que todavia no existe. Protegidos por su
        # propia validacion de credenciales/token, no por este gate -- mismo
        # criterio que /api/v1/auth/login|refresh arriba.
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/logout",
    }

    if request.method == "OPTIONS":
        return await call_next(request)

    if settings.rate_limit_enabled and request.url.path not in RATE_LIMIT_SKIP_PATHS:
        rate_result = _resolve_rate_limit(request)
        if rate_result is not None and not rate_result.allowed:
            return _too_many_requests(rate_result)

    if request.url.path not in public_paths and not _is_public_path(request.url.path):
        provided_key = request.headers.get("x-api-key")
        raw_authorization = request.headers.get("authorization")
        bearer_token = read_bearer_token(raw_authorization)

        has_api_key = bool(provided_key and provided_key == settings.api_key)
        has_user_token = False

        try:
            with open("auth_debug.log", "a", encoding="utf-8") as debug_file:
                debug_file.write(
                    f"[DEBUG req] path={request.url.path} has_auth_header={raw_authorization is not None} "
                    f"auth_len={len(raw_authorization) if raw_authorization else 0} has_api_key={has_api_key}\n"
                )
        except OSError:
            pass
        if bearer_token:
            # JWT propio (Fase 2) primero -- mismo orden que resolve_actor en
            # app/supabase_auth.py, y evita que un cliente que solo tiene el
            # JWT propio (sin x-api-key, caso de apps/mobile) quede bloqueado
            # aqui antes de que la ruta llegue a resolver la identidad.
            from app.jwt_auth import verify_access_token

            native_payload = verify_access_token(bearer_token)
            if native_payload:
                extra_headers = list(request.scope.get("headers", []))
                extra_headers.append((b"x-user-id", str(native_payload["sub"]).encode("utf-8")))
                if native_payload.get("role"):
                    extra_headers.append((b"x-user-role", str(native_payload["role"]).encode("utf-8")))
                request.scope["headers"] = extra_headers
                has_user_token = True
                bearer_token = None  # ya resuelto, salta la verificacion de Supabase de abajo

        if bearer_token:
            try:
                verify_supabase_token(bearer_token)
                identity = await get_supabase_identity(request.headers.get("authorization"))
                if identity:
                    extra_headers = list(request.scope.get("headers", []))
                    if identity.get("legacy_user_id") is not None:
                        extra_headers.append(
                            (
                                b"x-user-id",
                                str(identity["legacy_user_id"]).encode("utf-8"),
                            )
                        )
                    if identity.get("role"):
                        extra_headers.append((b"x-user-role", str(identity["role"]).encode("utf-8")))
                    if identity.get("assignment_code"):
                        extra_headers.append(
                            (
                                b"x-user-assignment-code",
                                str(identity["assignment_code"]).encode("utf-8"),
                            )
                        )
                    extra_headers.append((b"x-auth-user-id", str(identity["auth_user_id"]).encode("utf-8")))
                    request.scope["headers"] = extra_headers
                has_user_token = True
            except Exception as exc:
                debug_line = f"[DEBUG auth] token verification failed: {exc!r}\n"
                print(debug_line, end="")
                try:
                    with open("auth_debug.log", "a", encoding="utf-8") as debug_file:
                        debug_file.write(debug_line)
                except OSError:
                    pass
                has_user_token = False

        if not has_api_key and not has_user_token:
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "detail": "API key o JWT de Supabase invalido o ausente.",
                },
            )

    return await call_next(request)


app.include_router(health.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(data_sources.router, prefix="/api")
app.include_router(facturacion.router, prefix="/api")
app.include_router(customers.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(catalogs.router, prefix="/api")
app.include_router(readings.router, prefix="/api")
app.include_router(inspections.router, prefix="/api")
app.include_router(meters.router, prefix="/api")
app.include_router(local_services.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(customer_supplies.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(geocoding.router, prefix="/api")
app.include_router(gis.router, prefix="/api")
app.include_router(network.router, prefix="/api")
app.include_router(navigation.router, prefix="/api")
app.include_router(reports.router, prefix="/api")
app.include_router(supervisions.router, prefix="/api")
app.include_router(supervision_raw.router, prefix="/api")
app.include_router(supervision_table.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(sedapalgis.gis_router, prefix="/api")
app.include_router(sedapalgis.reports_router, prefix="/api")
app.include_router(sedapalgis_auth.router, prefix="/api")
app.include_router(sedapalgis.updater_router)
app.include_router(planillas.router, prefix="/api")
app.include_router(private_files.router, prefix="/api")

# NUEVO ROUTER PARA DOCUMENTOS PDF / HTML
app.include_router(documents.router, prefix="/api")
app.include_router(credit_notes.router, prefix="/api")

# Servidor propio de actualizaciones OTA (reemplaza EAS Update).
app.include_router(updates.router, prefix="/api")

SUPERVISION_MEDIA_ROOT = Path(settings.supervision_media_root)
SUPERVISION_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount(
    "/uploads/supervision-media",
    StaticFiles(directory=SUPERVISION_MEDIA_ROOT),
    name="supervision-media",
)

UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_ROOT), name="uploads")

UPDATER_RELEASES_ROOT = Path(settings.updater_releases_dir)
UPDATER_RELEASES_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/updater/files", StaticFiles(directory=UPDATER_RELEASES_ROOT), name="sedapalgis-updater")


@app.get("/health", tags=["health"])
async def healthcheck() -> dict[str, object]:
    return {"success": True, "service": "ok", "version": "backend-unified-v1"}
