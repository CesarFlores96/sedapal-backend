import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter(prefix="/v1", tags=["sedapalgis-proxy"])

ALLOWED_PREFIXES = ("auth/", "gis/")
FORWARDED_REQUEST_HEADERS = ("accept", "authorization", "content-type")


@router.api_route(
    "/{upstream_path:path}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def forward_to_sedapalgis(upstream_path: str, request: Request) -> Response:
    if not upstream_path.startswith(ALLOWED_PREFIXES):
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")

    client: httpx.AsyncClient = request.app.state.sedapalgis_client
    headers = {
        name: request.headers[name]
        for name in FORWARDED_REQUEST_HEADERS
        if name in request.headers
    }

    try:
        upstream_response = await client.request(
            request.method,
            f"/api/v1/{upstream_path}",
            params=request.query_params.multi_items(),
            headers=headers,
            content=await request.body(),
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="El servicio GIS local no está disponible.",
        ) from exc

    response_headers = {}
    if content_type := upstream_response.headers.get("content-type"):
        response_headers["content-type"] = content_type
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
