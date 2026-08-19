from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "BD Local API"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = Field(..., alias="DATABASE_URL")
    gis_database_url: str | None = Field(default=None, alias="GIS_DATABASE_URL")
    martin_database_url: str | None = Field(default=None, alias="MARTIN_DATABASE_URL")
    api_key: str = Field(..., alias="API_KEY")
    mobile_token_secret: str | None = Field(default=None, alias="MOBILE_TOKEN_SECRET")
    mobile_token_ttl_seconds: int = Field(default=60 * 60 * 12, alias="MOBILE_TOKEN_TTL_SECONDS")
    supabase_jwt_audience: str | None = Field(default="authenticated", alias="SUPABASE_JWT_AUDIENCE")
    supabase_jwt_issuer: str | None = Field(default=None, alias="SUPABASE_JWT_ISSUER")
    supabase_jwt_secret: str | None = Field(default=None, alias="SUPABASE_JWT_SECRET")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")
    supabase_database_url: str | None = Field(default=None, alias="SUPABASE_DATABASE_URL")
    private_files_root: str = Field(
        default=r"D:\BD_LOCAL\private-files", alias="PRIVATE_FILES_ROOT"
    )
    updater_releases_dir: str = Field(
        default=r"D:\BD_LOCAL\sedapalgis-releases", alias="UPDATER_RELEASES_DIR"
    )
    updater_public_base_url: str = Field(
        default="https://api.sedapal.lat", alias="UPDATER_PUBLIC_BASE_URL"
    )
    allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173,http://localhost:1420,http://127.0.0.1:1420,http://tauri.localhost,tauri://localhost",
        alias="ALLOWED_ORIGINS",
    )
    allowed_origin_regex: str | None = Field(
        default=None,
        alias="ALLOWED_ORIGIN_REGEX",
    )

    # --- Medios de supervision (fotos/videos) ---
    # Raiz en disco donde se guardan las evidencias de supervisiones, organizada
    # por mes/dia (carpeta OneDrive del equipo, sincronizada fuera del repo).
    supervision_media_root: str = Field(
        default=r"C:\Users\practicanteesce7\sedapal.com.pe\EQUIPO SERVICIOS Y CLIENTES ESPECIALES - 2026",
        alias="SUPERVISION_MEDIA_ROOT",
    )

    # --- OTA updates (self-hosted Expo Updates protocol) ---
    # Ruta en disco (fuera de cualquier repo git) a la clave privada RSA usada
    # para firmar manifests. Nunca debe apuntar a una ruta dentro de D:\Sedapal.
    ota_signing_private_key_path: str | None = Field(
        default=None, alias="OTA_SIGNING_PRIVATE_KEY_PATH"
    )
    # Base publica (tunel) usada para construir las URLs de assets/bundle en el
    # manifest, ej. "https://api.sedapal.lat".
    ota_public_base_url: str = Field(
        default="https://api.sedapal.lat", alias="OTA_PUBLIC_BASE_URL"
    )

    # --- Rate limiting (en memoria, un solo proceso) ---
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    # Login / endpoints públicos sensibles: estricto por IP.
    rate_limit_auth_max: int = Field(default=10, alias="RATE_LIMIT_AUTH_MAX")
    rate_limit_auth_window: int = Field(default=60, alias="RATE_LIMIT_AUTH_WINDOW_SECONDS")
    # App móvil / escritorio autenticada (por token de sesión).
    rate_limit_user_max: int = Field(default=1200, alias="RATE_LIMIT_USER_MAX")
    rate_limit_user_window: int = Field(default=60, alias="RATE_LIMIT_USER_WINDOW_SECONDS")
    # Web server-to-server con x-api-key: holgado (un origen concentra usuarios).
    rate_limit_trusted_max: int = Field(default=1200, alias="RATE_LIMIT_TRUSTED_MAX")
    rate_limit_trusted_window: int = Field(default=60, alias="RATE_LIMIT_TRUSTED_WINDOW_SECONDS")
    # Peticiones sin credenciales válidas (se rechazan igual con 401).
    rate_limit_anon_max: int = Field(default=120, alias="RATE_LIMIT_ANON_MAX")
    rate_limit_anon_window: int = Field(default=60, alias="RATE_LIMIT_ANON_WINDOW_SECONDS")

    # --- Navegacion vial (OSRM administrado) ---
    # El servicio no se expone al cliente movil: FastAPI es el unico consumidor.
    osrm_base_url: str | None = Field(default=None, alias="OSRM_BASE_URL")
    osrm_timeout_seconds: float = Field(default=12, alias="OSRM_TIMEOUT_SECONDS")
    osrm_max_stops: int = Field(default=50, alias="OSRM_MAX_STOPS")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def allowed_origin_regex_value(self) -> str | None:
        if not self.allowed_origin_regex:
            return None

        value = self.allowed_origin_regex.strip()
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
