"""Crea y guarda el rol remoto minimo para supervision y planillas."""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.windows_credentials import write_generic_credential

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


ROLE_NAME = "sedapal_boundary"
CREDENTIAL_NAME = "supabase-boundary-database-url.pe.sedapal"
ALLOWED_TABLES = {"supervision": "supervision_id", "planillas": "id"}


def connection_url_for_role(admin_url: str, password: str) -> str:
    parsed = urlsplit(admin_url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    tenant_suffix = ""
    if parsed.username and "." in parsed.username:
        tenant_suffix = "." + parsed.username.split(".", 1)[1]
    pooler_username = ROLE_NAME + tenant_suffix
    netloc = f"{quote(pooler_username)}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


async def configure() -> None:
    admin_url = get_settings().supabase_database_url
    if not admin_url:
        raise RuntimeError("Falta SUPABASE_DATABASE_URL administrativo para crear el rol restringido.")
    password = secrets.token_urlsafe(48)
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ROLE_NAME])
            exists = await cursor.fetchone()
            if exists:
                await cursor.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOINHERIT BYPASSRLS PASSWORD {}").format(
                    sql.Identifier(ROLE_NAME), sql.Literal(password)
                ))
            else:
                await cursor.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOINHERIT BYPASSRLS PASSWORD {}").format(
                    sql.Identifier(ROLE_NAME), sql.Literal(password)
                ))
            await cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(sql.Identifier(ROLE_NAME)))
            await cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(sql.Identifier(ROLE_NAME)))
            await cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(ROLE_NAME)))
            for table, identity_column in ALLOWED_TABLES.items():
                await cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(ROLE_NAME)
                ))
                await cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", [f"public.{table}", identity_column])
                sequence = await cursor.fetchone()
                if sequence and sequence[0]:
                    schema_name, sequence_name = sequence[0].split(".", 1)
                    await cursor.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {}.{} TO {}").format(
                        sql.Identifier(schema_name), sql.Identifier(sequence_name), sql.Identifier(ROLE_NAME)
                    ))
    write_generic_credential(CREDENTIAL_NAME, connection_url_for_role(admin_url, password), ROLE_NAME)
    print("Rol Supabase restringido y credencial Windows configurados correctamente.")


if __name__ == "__main__":
    asyncio.run(configure())
