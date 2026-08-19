# OSRM para navegación móvil

FastAPI consume este servicio en `127.0.0.1:5000`; el puerto no se publica a Internet.

1. Descargar el PBF de Perú o el extracto OSM aprobado para la zona operativa en `data/peru-latest.osm.pbf`.
2. Desde esta carpeta, preparar los datos una sola vez:

```powershell
docker run --rm -t -v "${PWD}\data:/data" osrm/osrm-backend:v5.27.1 osrm-extract -p /opt/car.lua /data/peru-latest.osm.pbf
docker run --rm -t -v "${PWD}\data:/data" osrm/osrm-backend:v5.27.1 osrm-partition /data/peru-latest.osrm
docker run --rm -t -v "${PWD}\data:/data" osrm/osrm-backend:v5.27.1 osrm-customize /data/peru-latest.osrm
```

3. Iniciar `docker compose up -d` y configurar `OSRM_BASE_URL=http://127.0.0.1:5000` en el `.env` de FastAPI.
4. Validar desde FastAPI con `POST /api/navigation/route` autenticado; nunca abrir el puerto 5000 ni incluir su URL en el APK.

Los archivos de `data/` son datos operativos descargados y no se versionan.
