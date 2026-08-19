import re
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg_pool import AsyncConnectionPool

from app.repositories.chat import fetch_chat_schema, fetch_chat_summary
from app.repositories.shared import fetch_all_dict


async def answer_chat(pool: AsyncConnectionPool, query: str) -> dict[str, str]:
    stats = await fetch_chat_summary(pool)
    return {
        "answer": (
            "La aplicacion ya esta conectada a FastAPI con PostgreSQL local. "
            f"Resumen actual: {stats['customers']} clientes, {stats['supplies']} suministros, "
            f"{stats['debts']} registros de facturacion y S/ {stats['pendingDebt']:.2f} en deuda pendiente. "
            f'Consulta recibida: "{query}". '
            "Si quieres respuestas analiticas especificas, el siguiente paso es conectar este endpoint "
            "a consultas guiadas sobre la base local."
        )
    }


async def get_chat_schema(pool: AsyncConnectionPool) -> dict[str, dict[str, list[dict[str, str]]]]:
    return {"schema": await fetch_chat_schema(pool)}


def validate_sql_query(sql: str) -> None:
    # Clean comments first to prevent bypasses
    clean_sql = re.sub(r"/\*[\s\S]*?\*/", "", sql)  # Remove multi-line comments
    clean_sql = re.sub(r"--.*$", "", clean_sql, flags=re.MULTILINE)  # Remove single-line comments
    clean_sql = clean_sql.strip().lower()

    if not clean_sql.startswith("select") and not clean_sql.startswith("with"):
        raise HTTPException(
            status_code=400,
            detail="Solo se permiten consultas de lectura (SELECT)."
        )

    # Forbidden keywords that indicate writing, modifying, or admin operations
    forbidden_keywords = [
        "insert",
        "update",
        "delete",
        "drop",
        "truncate",
        "alter",
        "create",
        "grant",
        "revoke",
        "replace",
        "vacuum",
        "analyze",
        "execute",
        "merge",
        "upsert",
    ]

    for keyword in forbidden_keywords:
        if re.search(r"\b" + keyword + r"\b", clean_sql):
            raise HTTPException(
                status_code=400,
                detail=f"La consulta generada contiene una palabra prohibida: \"{keyword}\". Solo se permiten consultas de lectura."
            )


async def run_query(pool: AsyncConnectionPool, sql: str) -> dict[str, list]:
    validate_sql_query(sql)
    try:
        results = await fetch_all_dict(pool, sql)
        # Use jsonable_encoder to safely serialize decimal, datetime.date, etc.
        serialized_results = jsonable_encoder(results)
        return {"results": serialized_results}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al ejecutar la consulta SQL: {str(e)}"
        )
