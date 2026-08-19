from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = ROOT / "scripts" / "sql" / "001_supabase_local_boundary.sql"
DEFAULT_PRIVATE_ROOT = Path(r"D:\BD_LOCAL\private-files")
DEFAULT_REPORT_ROOT = Path(r"D:\BD_LOCAL\backups_google\backend-unification")

REMOTE_WINS = (
    "profiles",
    "users",
    "roles",
    "app_views",
    "permissions",
    "role_permissions",
    "user_view_permissions",
    "user_roles",
    "derivation_recipient_contacts",
    "profile_manual_signatures",
    "profile_signature_certificates",
    "push_tokens",
    "push_notification_outbox",
    "supervision_signatures",
    "supervision_code_catalog",
)
HISTORY_MERGE = (
    "anomalies",
    "customer_debts",
    "customer_payments",
    "fp_debt_snapshots",
)
FORBIDDEN_REMOTE_TABLES = frozenset({"supervision", "planillas"})
CONFLICT_METADATA_COLUMNS = {
    # Ambas copias contienen el mismo evento; esta fecha refleja la carga en
    # cada base, no una actualizacion funcional que permita elegir un ganador.
    "anomalies": frozenset({"created_at"}),
    "customer_payments": frozenset({"created_at"}),
    "fp_debt_snapshots": frozenset({"created_at"}),
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def require(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"Falta configurar {name}.")
    return value


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def database_value(value: Any) -> Any:
    """Preserva tipos escalares y adapta objetos JSON para psycopg."""
    if isinstance(value, dict):
        return Jsonb(value)
    return value


def columns(connection: psycopg.Connection[Any], table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
              AND is_generated = 'NEVER'
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [row[0] for row in cursor.fetchall()]


def primary_key(connection: psycopg.Connection[Any], table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT attribute.attname
            FROM pg_index index_definition
            JOIN pg_class relation ON relation.oid = index_definition.indrelid
            JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
            JOIN unnest(index_definition.indkey) WITH ORDINALITY key(attnum, ordinality) ON true
            JOIN pg_attribute attribute
              ON attribute.attrelid = relation.oid AND attribute.attnum = key.attnum
            WHERE namespace.nspname = 'public'
              AND relation.relname = %s
              AND index_definition.indisprimary
            ORDER BY key.ordinality
            """,
            (table,),
        )
        return [row[0] for row in cursor.fetchall()]


def count_rows(connection: psycopg.Connection[Any], table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table)))
        return int(cursor.fetchone()[0])


def conflicting_keys(
    remote: psycopg.Connection[Any], local: psycopg.Connection[Any], table: str, key_columns: list[str]
) -> int:
    if not key_columns:
        raise RuntimeError(f"{table} no tiene clave primaria; se cancela para evitar duplicados.")
    remote_rows: list[tuple[Any, ...]]
    with remote.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {} FROM public.{}").format(
                sql.SQL(", ").join(map(sql.Identifier, key_columns)), sql.Identifier(table)
            )
        )
        remote_rows = cursor.fetchall()
    if not remote_rows:
        return 0
    existing = 0
    predicate = sql.SQL(" AND ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in key_columns
    )
    query = sql.SQL("SELECT 1 FROM public.{} WHERE {} LIMIT 1").format(
        sql.Identifier(table), predicate
    )
    with local.cursor() as cursor:
        for row in remote_rows:
            cursor.execute(query, row)
            existing += int(cursor.fetchone() is not None)
    return existing


def mismatched_conflicts(
    remote: psycopg.Connection[Any],
    local: psycopg.Connection[Any],
    table: str,
    key_columns: list[str],
    common_columns: list[str],
) -> int:
    comparison_columns = [
        column
        for column in common_columns
        if column not in CONFLICT_METADATA_COLUMNS.get(table, frozenset())
    ]
    select_columns = sql.SQL(", ").join(map(sql.Identifier, comparison_columns))
    predicate = sql.SQL(" AND ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in key_columns
    )
    local_query = sql.SQL("SELECT {} FROM public.{} WHERE {}").format(
        select_columns, sql.Identifier(table), predicate
    )
    mismatches = 0
    with remote.cursor(row_factory=dict_row) as source, local.cursor() as target:
        source.execute(sql.SQL("SELECT {} FROM public.{}").format(select_columns, sql.Identifier(table)))
        for remote_row in source:
            target.execute(local_query, tuple(remote_row[column] for column in key_columns))
            local_row = target.fetchone()
            if local_row is not None and tuple(
                remote_row[column] for column in comparison_columns
            ) != tuple(local_row):
                mismatches += 1
    return mismatches


def migrate_table(
    remote: psycopg.Connection[Any],
    local: psycopg.Connection[Any],
    table: str,
    *,
    remote_wins: bool,
) -> dict[str, Any]:
    if table in FORBIDDEN_REMOTE_TABLES:
        raise RuntimeError(f"La frontera impide migrar la tabla remota {table}.")

    common = [column for column in columns(remote, table) if column in set(columns(local, table))]
    common = [column for column in common if column not in {"embedding", "embedding_source_text"}]
    keys = primary_key(local, table)
    if not common or not keys:
        raise RuntimeError(f"No se pudo determinar columnas/PK compatibles para {table}.")

    before = count_rows(local, table)
    remote_count = count_rows(remote, table)
    conflicts = conflicting_keys(remote, local, table, keys)

    if not remote_wins and conflicts and "updated_at" not in common:
        mismatches = mismatched_conflicts(remote, local, table, keys, common)
        if mismatches:
            raise RuntimeError(
                f"{table} tiene {mismatches} conflictos distintos y no posee updated_at; no se sobrescribio nada."
            )

    column_sql = sql.SQL(", ").join(map(sql.Identifier, common))
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in common)
    conflict_columns = sql.SQL(", ").join(map(sql.Identifier, keys))
    update_columns = [column for column in common if column not in keys]

    if update_columns and remote_wins:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in update_columns
        )
        conflict_action = sql.SQL("DO UPDATE SET {} ").format(assignments)
    elif update_columns and "updated_at" in common:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in update_columns
        )
        conflict_action = sql.SQL(
            "DO UPDATE SET {} WHERE EXCLUDED.updated_at > {}.updated_at "
        ).format(assignments, sql.Identifier(table))
    else:
        conflict_action = sql.SQL("DO NOTHING ")

    insert_query = sql.SQL(
        "INSERT INTO public.{} ({}) VALUES ({}) ON CONFLICT ({}) {}"
    ).format(sql.Identifier(table), column_sql, placeholders, conflict_columns, conflict_action)

    batch_size = 1000
    migrated = 0
    with remote.cursor(name=f"migrate_{table}") as source:
        source.itersize = batch_size
        source.execute(sql.SQL("SELECT {} FROM public.{}").format(column_sql, sql.Identifier(table)))
        while rows := source.fetchmany(batch_size):
            adapted_rows = [tuple(database_value(value) for value in row) for row in rows]
            with local.cursor() as target:
                target.executemany(insert_query, adapted_rows)
            local.commit()
            migrated += len(rows)

    after = count_rows(local, table)
    if table == "supervision_signatures" and "id" in common:
        # Se importan IDs historicos de forma explicita. PostgreSQL no avanza
        # automaticamente la secuencia identity al hacer ese tipo de INSERT.
        with local.cursor() as cursor:
            cursor.execute(
                """
                SELECT setval(
                  pg_get_serial_sequence('public.supervision_signatures', 'id'),
                  COALESCE((SELECT max(id) FROM public.supervision_signatures), 1),
                  EXISTS (SELECT 1 FROM public.supervision_signatures)
                )
                """
            )
        local.commit()
    return {
        "table": table,
        "remote_rows": remote_count,
        "local_before": before,
        "local_after": after,
        "source_rows_processed": migrated,
        "preexisting_keys": conflicts,
        "strategy": "remote_wins" if remote_wins else "history_merge",
    }


def migrate_profile_extensions(
    remote: psycopg.Connection[Any], local: psycopg.Connection[Any]
) -> dict[str, Any]:
    with remote.cursor() as source:
        source.execute(
            """
            SELECT id, email, username, assignment_code, legacy_user_id,
                   last_login_at, source_system, updated_at
            FROM public.profiles
            ORDER BY id
            """
        )
        rows = source.fetchall()
    with local.cursor() as target:
        target.executemany(
            """
            INSERT INTO public.auth_profiles_local (
              auth_user_id, email, username, assignment_code, legacy_user_id,
              last_login_at, source_system, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (auth_user_id) DO UPDATE SET
              email = EXCLUDED.email,
              username = EXCLUDED.username,
              assignment_code = EXCLUDED.assignment_code,
              legacy_user_id = EXCLUDED.legacy_user_id,
              last_login_at = EXCLUDED.last_login_at,
              source_system = EXCLUDED.source_system,
              updated_at = EXCLUDED.updated_at
            """,
            rows,
        )
    local.commit()
    return {"table": "auth_profiles_local", "source_rows_processed": len(rows), "strategy": "remote_wins"}


def reset_local_access_configuration(local: psycopg.Connection[Any]) -> None:
    """Supabase gana una sola vez para la configuracion de acceso.

    Se borran primero las relaciones para conservar las FK existentes. No se
    incluyen users/profiles ni ninguna tabla operativa.
    """
    with local.cursor() as cursor:
        for table in (
            "user_view_permissions",
            "user_roles",
            "role_permissions",
            "permissions",
            "roles",
            "app_views",
        ):
            cursor.execute(sql.SQL("DELETE FROM public.{}").format(sql.Identifier(table)))
    local.commit()


def safe_relative_path(bucket: str, object_path: str) -> Path:
    normalized = object_path.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    if not parts or any(not re.fullmatch(r"[^\x00]+", part) for part in parts):
        raise RuntimeError(f"Ruta de Storage invalida: {bucket}/{object_path}")
    return Path(bucket, *parts)


def migrate_storage(
    remote: psycopg.Connection[Any],
    local: psycopg.Connection[Any],
    *,
    supabase_url: str,
    service_role_key: str,
    private_root: Path,
) -> dict[str, Any]:
    private_root.mkdir(parents=True, exist_ok=True)
    with remote.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT bucket_id, name, metadata, updated_at
            FROM storage.objects
            ORDER BY bucket_id, name
            """
        )
        objects = cursor.fetchall()

    headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
    downloaded = 0
    total_bytes = 0
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), headers=headers) as client:
        for item in objects:
            bucket = str(item["bucket_id"])
            object_path = str(item["name"])
            relative = safe_relative_path(bucket, object_path)
            destination = (private_root / relative).resolve()
            if private_root.resolve() not in destination.parents:
                raise RuntimeError(f"Ruta fuera de PRIVATE_FILES_ROOT: {destination}")
            response = client.get(
                f"{supabase_url.rstrip('/')}/storage/v1/object/authenticated/"
                f"{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
            )
            response.raise_for_status()
            content = response.content
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            metadata = item.get("metadata") or {}
            content_type = metadata.get("mimetype") or response.headers.get("content-type")
            with local.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.private_file_objects (
                      bucket, object_path, local_relative_path, content_type,
                      size_bytes, sha256, source_updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bucket, object_path) DO UPDATE SET
                      local_relative_path = EXCLUDED.local_relative_path,
                      content_type = EXCLUDED.content_type,
                      size_bytes = EXCLUDED.size_bytes,
                      sha256 = EXCLUDED.sha256,
                      source_updated_at = EXCLUDED.source_updated_at,
                      migrated_at = now()
                    """,
                    (
                        bucket,
                        object_path,
                        relative.as_posix(),
                        content_type,
                        len(content),
                        digest,
                        item.get("updated_at"),
                    ),
                )
            local.commit()
            downloaded += 1
            total_bytes += len(content)
    return {"objects": downloaded, "bytes": total_bytes, "root": str(private_root)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migra datos operativos fuera de Supabase.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-storage", action="store_true")
    args = parser.parse_args()

    backend_env = read_env(ROOT / ".env")
    web_env = read_env(Path(r"D:\Sedapal\apps\web\.env"))
    local_url = require(backend_env.get("DATABASE_URL"), "DATABASE_URL")
    remote_url = require(backend_env.get("SUPABASE_DATABASE_URL"), "SUPABASE_DATABASE_URL")
    supabase_url = require(
        backend_env.get("SUPABASE_URL") or web_env.get("NEXT_PUBLIC_SUPABASE_URL"),
        "SUPABASE_URL",
    )
    service_key = require(web_env.get("SUPABASE_SERVICE_ROLE_KEY"), "SUPABASE_SERVICE_ROLE_KEY")
    private_root = Path(backend_env.get("PRIVATE_FILES_ROOT", str(DEFAULT_PRIVATE_ROOT)))
    report_root = Path(backend_env.get("BOUNDARY_REPORT_ROOT", str(DEFAULT_REPORT_ROOT)))
    report_root.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "dry_run": args.dry_run,
        "tables": [],
    }
    with psycopg.connect(local_url) as local, psycopg.connect(remote_url) as remote:
        if args.dry_run:
            for table in (*REMOTE_WINS, *HISTORY_MERGE):
                report["tables"].append(
                    {"table": table, "remote_rows": count_rows(remote, table), "strategy": "preview"}
                )
        else:
            with local.cursor() as cursor:
                cursor.execute(DDL_PATH.read_text(encoding="utf-8"))
            local.commit()
            reset_local_access_configuration(local)
            for table in REMOTE_WINS:
                report["tables"].append(migrate_table(remote, local, table, remote_wins=True))
                if table == "profiles":
                    report["tables"].append(migrate_profile_extensions(remote, local))
            for table in HISTORY_MERGE:
                report["tables"].append(migrate_table(remote, local, table, remote_wins=False))
            if not args.skip_storage:
                report["storage"] = migrate_storage(
                    remote,
                    local,
                    supabase_url=supabase_url,
                    service_role_key=service_key,
                    private_root=private_root,
                )
            report["completed_at"] = datetime.now().astimezone().isoformat()
            with local.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.supabase_boundary_migrations (migration_key, report)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (migration_key) DO UPDATE SET report = EXCLUDED.report, completed_at = now()
                    """,
                    ("2026-08-10-supabase-local-boundary", json.dumps(report, default=json_value)),
                )
            local.commit()

    report_path = report_root / f"supabase-boundary-{datetime.now():%Y%m%d-%H%M%S}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=json_value), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "dry_run": args.dry_run}, ensure_ascii=False))


if __name__ == "__main__":
    main()
