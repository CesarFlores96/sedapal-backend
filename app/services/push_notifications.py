"""Envio de push notifications (Expo Push Service) para avisos que no
dependen de la cola/trigger de Supabase (05_push_notifications.sql), como el
aviso de "hay una actualizacion OTA disponible" tras un publish exitoso.

Usa `urllib.request` (stdlib) en vez de agregar una libreria HTTP nueva al
proyecto (httpx/requests): es un solo POST fire-and-forget, no vale la pena
la dependencia extra.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

EXPO_PUSH_API_URL = "https://exp.host/--/api/v2/push/send"


def _post_expo_push_messages(messages: list[dict]) -> None:
    if not messages:
        return

    payload = json.dumps(messages).encode("utf-8")
    request = urllib.request.Request(
        EXPO_PUSH_API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.URLError as exc:
        # No debe tumbar el publish si Expo Push esta caido; solo se loguea.
        print(f"[push] fallo al notificar actualizacion OTA disponible: {exc!r}")


async def notify_ota_update_available(
    supabase_pool: AsyncConnectionPool, *, runtime_version: str
) -> None:
    try:
        async with supabase_pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SELECT DISTINCT expo_push_token FROM public.push_tokens;")
                rows = await cursor.fetchall()
    except Exception as exc:  # tabla aun no migrada, o Supabase no alcanzable: no bloquea el publish
        print(f"[push] no se pudo leer push_tokens (¿falta la migracion 025?): {exc!r}")
        return

    tokens = [row["expo_push_token"] for row in rows if row.get("expo_push_token")]
    if not tokens:
        return

    messages = [
        {
            "to": token,
            "title": "Nueva actualizacion disponible",
            "body": "Hay una nueva version de la app. Toca para aplicarla.",
            "priority": "default",
            "sound": "default",
            "data": {"type": "ota_update_available", "runtimeVersion": runtime_version},
        }
        for token in tokens
    ]

    await asyncio.to_thread(_post_expo_push_messages, messages)
