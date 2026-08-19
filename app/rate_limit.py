"""Rate limiting en memoria para el API FastAPI.

Diseñado para un único proceso uvicorn local (sin infraestructura extra). Usa
una ventana deslizante por clave: se guardan los timestamps de las peticiones
recientes y se descartan las que caen fuera de la ventana.

Niveles (tiers):
- ``auth``: login/endpoints públicos sensibles. Límite estricto por IP para
  frenar fuerza bruta.
- ``trusted``: peticiones con ``x-api-key`` válido (la web server-to-server).
  Límite holgado por IP porque un solo origen concentra a muchos usuarios.
- ``user``: peticiones con JWT de Supabase (app móvil). Límite por token, así
  cada sesión tiene su propio presupuesto.
- ``anon``: cualquier otra cosa; recibe 401 igual, pero se limita para no
  gastar CPU verificando basura.

No es distribuido: si algún día se corre multi-worker o se mueve a serverless
hay que cambiar el backend a Redis/Upstash. Ver nota en el vault.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset: int


class SlidingWindowRateLimiter:
    """Limitador de ventana deslizante, seguro para uso concurrente."""

    def __init__(self, cleanup_interval_seconds: int = 300) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval_seconds
        self._last_cleanup = time.monotonic()

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.monotonic()
        window_start = now - window_seconds

        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket

            # Descarta timestamps fuera de la ventana.
            while bucket and bucket[0] <= window_start:
                bucket.popleft()

            current = len(bucket)

            if current >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                self._maybe_cleanup(now, window_seconds)
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    retry_after=retry_after,
                    reset=retry_after,
                )

            bucket.append(now)
            remaining = limit - (current + 1)
            self._maybe_cleanup(now, window_seconds)
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=remaining,
                retry_after=0,
                reset=window_seconds,
            )

    def _maybe_cleanup(self, now: float, window_seconds: int) -> None:
        """Elimina claves sin actividad reciente para acotar la memoria."""
        if now - self._last_cleanup < self._cleanup_interval:
            return

        self._last_cleanup = now
        threshold = now - window_seconds
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= threshold]
        for key in stale:
            del self._hits[key]


_limiter = SlidingWindowRateLimiter()


def hash_token(token: str) -> str:
    """Deriva una clave estable y no reversible a partir del bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def check(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    return _limiter.check(key, limit, window_seconds)
