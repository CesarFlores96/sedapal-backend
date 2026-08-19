from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.media_storage import supervision_media_dir
from app.services.pdf_service import (
    generar_pdf_desde_html,
    render_orden_servicio_html,
)


router = APIRouter(prefix="/documents", tags=["documents"])


class HtmlToPdfRequest(BaseModel):
    html: str = Field(..., min_length=1, max_length=5_000_000)
    filename: str | None = None
    work_order_number: str | None = Field(default=None, alias="workOrderNumber")


async def obtener_orden_servicio(numero_servicio: str) -> dict | None:
    """
    Temporalmente devuelve datos de prueba.
    Luego aquí conectarás con tu repository o consulta PostgreSQL.
    """

    return {
        "numero_servicio": numero_servicio,
        "nis": "3100475",
        "generada_por": "SD013841",
        "cus": "54442170",
        "cup": "021-00652-013",
        "oficina_comercial": "GRANDES CLIENTES",
        "ruta": "GRAN CLIENTE 21",
        "itinerario": "GRAN CLIENTE 21-42",
        "tipo_suministro": "UNIFAMILIAR",
        "fecha_emision": "22/06/2026 09:47",
        "fecha_resolucion": "20/06/2026",
        "actividad_solicitada": "Supervision de Sedapal (Manual)",
        "observaciones": "Disminucion de consumo",
        "detalle_observacion": "Situacion predio servicio caja medidor lectura",
        "contratista": "",
        "reclamo": "",
        "expediente": "",
        "razon_social": "COLEGIO PARTICULAR DE JESUS",
        "ruc": "20109174841",
        "telefono": "4630156",
        "direccion": "AV BRASIL 2470 2478",
        "urbanizacion": "CERC CERCADO",
        "distrito": "PUEBLO LIBRE",
        "referencia": "COLEGIO DE JESUS",
        "acceso_inmueble": "",
        "cua": "0604",
        "actividad_predio": "COLEGIO PARTICULAR",
        "medidor": "FF24000306",
        "diametro": "50.00",
        "ultima_lectura": "24254",
        "dispositivo_seguridad": "Anclaje con Argolla",
        "punto_medida": "F2 AV BRASIL 2478 DER",
        "acometidas_asociadas": "1",
        "ubicacion_conexion": "En la vereda",
        "pisos": "3",
        "codigo_abastecimiento": "PUE002 00",
        "horario_abastecimiento": "DIARI 00- 24",
        "fuente": "021-00652-0130-02-",
        "cota": "1.8",
    }


@router.get("/orden-servicio/{numero_servicio}/html", response_class=HTMLResponse)
async def preview_orden_servicio(numero_servicio: str):
    orden = await obtener_orden_servicio(numero_servicio)

    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    html = render_orden_servicio_html(orden)

    return HTMLResponse(content=html)


@router.get("/orden-servicio/{numero_servicio}/pdf")
async def emitir_orden_servicio_pdf(numero_servicio: str):
    orden = await obtener_orden_servicio(numero_servicio)

    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    html = render_orden_servicio_html(orden)
    pdf_bytes = await generar_pdf_desde_html(html)

    filename = f"orden_servicio_{numero_servicio}.pdf"

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        },
    )


@router.post("/pdf-from-html")
async def convertir_html_a_pdf(payload: HtmlToPdfRequest):
    """
    Convierte un HTML arbitrario (ya renderizado por el llamante, p.ej. la web
    de Next.js con su propia plantilla) a PDF usando Playwright. Genérico: no
    depende de datos de supervisión ni de ningún otro dominio.
    """

    pdf_bytes = await generar_pdf_desde_html(payload.html)
    filename = payload.filename or "documento.pdf"

    if payload.work_order_number:
        # Guarda una copia del PDF derivado junto a las fotos/videos de esa OS.
        target_dir = supervision_media_dir(payload.work_order_number)
        (target_dir / filename).write_bytes(pdf_bytes)

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        },
    )