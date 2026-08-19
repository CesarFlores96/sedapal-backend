"""Copia supply_embeddings desde el respaldo remoto hacia PostgreSQL local."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings


SQL_PATH = Path(__file__).resolve().parent / "sql" / "003_local_supply_embeddings.sql"


def parse_embedding(value: str) -> list[float]:
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, list) or len(parsed) != 384:
        raise ValueError("Embedding remoto invalido.")
    return [float(item) for item in parsed]


def main() -> None:
    settings = get_settings()
    if not settings.supabase_database_url:
        raise RuntimeError("Falta SUPABASE_DATABASE_URL para la migracion inicial.")

    with psycopg.connect(settings.database_url) as local_connection:
        with local_connection.cursor() as local_cursor:
            local_cursor.execute(SQL_PATH.read_text(encoding="utf-8"))
        local_connection.commit()

        with psycopg.connect(settings.supabase_database_url) as remote_connection:
            with remote_connection.cursor() as remote_cursor:
                remote_cursor.execute(
                    """
                    SELECT supply_code, embedding::text, embedding_source_text,
                           created_at, updated_at
                    FROM public.supply_embeddings
                    ORDER BY supply_code;
                    """
                )

                copied = 0
                while rows := remote_cursor.fetchmany(250):
                    payload = [
                        (code, parse_embedding(embedding), source_text, created_at, updated_at)
                        for code, embedding, source_text, created_at, updated_at in rows
                    ]
                    with local_connection.cursor() as local_cursor:
                        local_cursor.executemany(
                            """
                            INSERT INTO public.supply_embeddings (
                              supply_code, embedding, embedding_source_text,
                              created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (supply_code) DO UPDATE SET
                              embedding = EXCLUDED.embedding,
                              embedding_source_text = EXCLUDED.embedding_source_text,
                              updated_at = EXCLUDED.updated_at;
                            """,
                            payload,
                        )
                    local_connection.commit()
                    copied += len(payload)

        with local_connection.cursor() as local_cursor:
            local_cursor.execute("SELECT count(*) FROM public.supply_embeddings")
            local_count = int(local_cursor.fetchone()[0])

    if copied != local_count:
        raise RuntimeError(f"Conteo inconsistente: remoto={copied}, local={local_count}.")
    print(f"Migracion local de embeddings completada: {local_count} filas.")


if __name__ == "__main__":
    main()
