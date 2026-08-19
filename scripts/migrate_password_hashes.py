"""Migracion de contrasenas Fase 2 (una sola vez): copia el hash bcrypt real
de cada usuario desde `auth.users.encrypted_password` en Supabase hacia
`public.users.password_hash` en el Postgres local, para los usuarios que ya
tienen un mapeo en `public.auth_profiles_local` (auth_user_id -> legacy_user_id).

No re-hashea nada: el hash bcrypt se copia tal cual. `app/passwords.py` ya
distingue el algoritmo por el prefijo del hash (`scrypt$...` vs `$2[aby]$...`),
asi que el login sigue funcionando sin que el usuario note nada -- su
contrasena actual real (la de Supabase) pasa a ser la que valida localmente.

Requiere las variables de entorno SUPABASE_DATABASE_URL y DATABASE_URL (las
mismas que ya usa FastAPI, se leen del .env generado por fetch_secrets.sh).

Uso: python3 scripts/migrate_password_hashes.py [--dry-run]
"""

import asyncio
import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    dry_run = "--dry-run" in sys.argv

    supabase_url = os.environ.get("SUPABASE_DATABASE_URL")
    local_url = os.environ.get("DATABASE_URL")
    if not supabase_url or not local_url:
        print("ERROR: faltan SUPABASE_DATABASE_URL o DATABASE_URL en el entorno.", file=sys.stderr)
        sys.exit(1)

    async with await psycopg.AsyncConnection.connect(supabase_url) as supabase_conn:
        async with supabase_conn.cursor() as cur:
            await cur.execute(
                "SELECT id, email, encrypted_password FROM auth.users WHERE encrypted_password IS NOT NULL;"
            )
            supabase_users = await cur.fetchall()

    print(f"Usuarios con password en Supabase: {len(supabase_users)}")

    async with await psycopg.AsyncConnection.connect(local_url) as local_conn:
        async with local_conn.cursor() as cur:
            await cur.execute(
                "SELECT auth_user_id::text, legacy_user_id FROM public.auth_profiles_local WHERE legacy_user_id IS NOT NULL;"
            )
            mapping_rows = await cur.fetchall()
        mapping = {str(auth_user_id): legacy_user_id for auth_user_id, legacy_user_id in mapping_rows}
        print(f"Mapeos auth_user_id -> legacy_user_id disponibles: {len(mapping)}")

        migrated = 0
        skipped_no_mapping = 0
        skipped_bad_hash = 0

        async with local_conn.cursor() as cur:
            for auth_user_id, email, encrypted_password in supabase_users:
                legacy_user_id = mapping.get(str(auth_user_id))
                if legacy_user_id is None:
                    skipped_no_mapping += 1
                    print(f"  [sin mapeo] {email} ({auth_user_id})")
                    continue

                if not encrypted_password.startswith(("$2a$", "$2b$", "$2y$")):
                    skipped_bad_hash += 1
                    print(f"  [hash no-bcrypt, se ignora] {email} ({auth_user_id})")
                    continue

                if dry_run:
                    print(f"  [dry-run] migraria password de {email} -> legacy_user_id={legacy_user_id}")
                    migrated += 1
                    continue

                await cur.execute(
                    "UPDATE public.users SET password_hash = %s, updated_at = NOW() WHERE id = %s;",
                    (encrypted_password, legacy_user_id),
                )
                migrated += 1
                print(f"  [OK] {email} -> legacy_user_id={legacy_user_id}")

        if not dry_run:
            await local_conn.commit()

    print(
        f"\nResumen: {migrated} migrados, {skipped_no_mapping} sin mapeo, "
        f"{skipped_bad_hash} con hash no-bcrypt (OAuth/magic-link, no tienen password)."
        + (" [DRY RUN, no se escribio nada]" if dry_run else "")
    )


if __name__ == "__main__":
    asyncio.run(main())
