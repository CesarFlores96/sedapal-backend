import asyncio
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"


env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render_orden_servicio_html(orden: dict) -> str:
    template = env.get_template("orden_servicio.html")
    return template.render(orden=orden)


async def _generar_pdf_playwright(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        try:
            page = await browser.new_page(
                viewport={
                    "width": 1120,
                    "height": 1588,
                }
            )

            await page.set_content(
                html,
                wait_until="load",
            )

            await page.emulate_media(media="print")

            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                scale=1,
                margin={
                    "top": "0mm",
                    "right": "0mm",
                    "bottom": "0mm",
                    "left": "0mm",
                },
            )

            return pdf_bytes

        finally:
            await browser.close()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )

        try:
            page = await browser.new_page(
                viewport={
                    "width": 1120,
                    "height": 1588,
                }
            )

            await page.set_content(
                html,
                wait_until="load",
            )

            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                margin={
                    "top": "0mm",
                    "right": "0mm",
                    "bottom": "0mm",
                    "left": "0mm",
                },
            )

            return pdf_bytes

        finally:
            await browser.close()


def _generar_pdf_en_hilo_windows(html: str) -> bytes:
    """
    En Windows, el proyecto usa SelectorEventLoop para PostgreSQL/psycopg.
    Playwright necesita ProactorEventLoop para poder lanzar Chromium.
    Por eso generamos el PDF en un hilo separado con su propio event loop.
    """

    loop = asyncio.ProactorEventLoop()

    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_generar_pdf_playwright(html))

    finally:
        loop.close()


async def generar_pdf_desde_html(html: str) -> bytes:
    if sys.platform == "win32":
        return await asyncio.to_thread(_generar_pdf_en_hilo_windows, html)

    return await _generar_pdf_playwright(html)