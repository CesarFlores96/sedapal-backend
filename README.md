# API FastAPI local

La documentacion operativa completa esta en `D:\BD_LOCAL\README.md`.

Puntos clave:

- La API se ejecuta en `127.0.0.1:8000`.
- PostgreSQL debe permanecer escuchando solo en `127.0.0.1`.
- Todas las solicitudes a `/api/*` requieren `x-api-key`.
- Ejemplo principal: `GET /api/facturacion?suministro=XXXXX&page=1&page_size=100`.
# Proxy SEDAPAL GIS

El túnel público existente termina en este servicio (`127.0.0.1:8000`). Las rutas `/api/v1/auth/*` y `/api/v1/gis/*` se reenvían exclusivamente al backend GIS local configurado por `SEDAPALGIS_UPSTREAM_URL` (por defecto `http://127.0.0.1:8010`). La base PostgreSQL no se publica y permanece en loopback.
