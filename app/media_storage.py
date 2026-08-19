import io
import re
from datetime import date, datetime
from pathlib import Path

import piexif

from app.config import get_settings

MESES_ES = {
    1: "Enero",
    2: "Febrero",
    3: "Marzo",
    4: "Abril",
    5: "Mayo",
    6: "Junio",
    7: "Julio",
    8: "Agosto",
    9: "Septiembre",
    10: "Octubre",
    11: "Noviembre",
    12: "Diciembre",
}

# Extensiones sobre las que se puede escribir EXIF con piexif (solo JPEG).
_EXIF_CAPABLE_EXTENSIONS = {".jpg", ".jpeg"}


def sanitize_folder_segment(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value).strip())
    return cleaned or "sin-dato"


def _month_folder(today: date) -> str:
    return f"{MESES_ES[today.month]}_{today.year}"


def _day_folder(today: date) -> str:
    return f"{today.day:02d}_{today.month:02d}_{today.year}"


def supervision_media_dir(
    work_order_number: str,
    *,
    storage_date: date | None = None,
) -> Path:
    effective_date = storage_date or date.today()
    root = Path(get_settings().supervision_media_root)
    target_dir = root / _month_folder(effective_date) / _day_folder(effective_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _signed_pdf_filename(work_order_number: str | None, supply_code: str | None) -> str:
    """Nombre operativo del PDF firmado.

    La OS identifica las supervisiones programadas. Para una supervision sin OS,
    el suministro es la referencia que necesita el equipo al revisar la carpeta.
    """

    identifier = work_order_number or supply_code
    if not identifier:
        raise ValueError("No se puede nombrar un PDF firmado sin OS ni suministro.")
    return f"{sanitize_folder_segment(str(identifier))}.pdf"


def supervision_signed_pdf_path(
    work_order_number: str | None,
    supply_code: str | None,
) -> Path:
    """Ruta del PDF firmado en la carpeta OneDrive del dia actual."""

    today = date.today()
    root = Path(get_settings().supervision_media_root)
    target_dir = root / _month_folder(today) / _day_folder(today)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / _signed_pdf_filename(work_order_number, supply_code)


def find_supervision_signed_pdf(
    work_order_number: str | None,
    supply_code: str | None,
    supervision_id: int,
) -> Path | None:
    """Encuentra el PDF firmado mas reciente, incluso si fue firmado otro dia."""

    root = Path(get_settings().supervision_media_root)
    filename = _signed_pdf_filename(work_order_number, supply_code)
    matches = [path for path in root.glob(f"*_*/*_*_*/{filename}") if path.is_file()]
    if matches:
        return max(matches, key=lambda path: path.stat().st_mtime)

    # Compatibilidad de lectura con el formato usado antes de la organizacion
    # por mes y dia. No se vuelve a escribir en esa ubicacion.
    legacy_path = root / "PDF_Firmados" / f"id-{supervision_id}" / "current.pdf"
    return legacy_path if legacy_path.is_file() else None


def supervision_media_url(
    work_order_number: str,
    file_name: str,
    *,
    storage_date: date | None = None,
) -> str:
    effective_date = storage_date or date.today()
    return (
        f"/uploads/supervision-media/{_month_folder(effective_date)}/"
        f"{_day_folder(effective_date)}/{file_name}"
    )


def build_sequential_media_filename(target_dir: Path, supply_code: str, extension: str) -> str:
    """Nombra el archivo como `{suministro}_{n}{ext}`, con `n` el siguiente
    numero disponible para ese suministro dentro de la carpeta del dia (cuenta
    fotos y videos juntos, sin importar la extension, para no repetir indice)."""

    prefix = sanitize_folder_segment(supply_code)
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)\.")

    highest = 0
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        match = pattern.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))

    return f"{prefix}_{highest + 1}{extension}"


def _decimal_to_dms_rational(value: float) -> tuple:
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 100)
    return ((degrees, 1), (minutes, 1), (seconds, 100))


def embed_photo_metadata(
    content: bytes,
    extension: str,
    *,
    latitude: float | None,
    longitude: float | None,
    captured_at: datetime | None,
) -> bytes:
    """Escribe coordenadas GPS y fecha/hora de captura en el EXIF del JPEG.
    Si el archivo no es JPEG o no trae metadata para grabar, devuelve el
    contenido tal cual (sin fallar la subida por esto)."""

    if extension.lower() not in _EXIF_CAPABLE_EXTENSIONS:
        return content
    if latitude is None and longitude is None and captured_at is None:
        return content

    try:
        exif_dict = piexif.load(content)
    except Exception:
        return content

    if captured_at is not None:
        formatted = captured_at.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict.setdefault("0th", {})[piexif.ImageIFD.DateTime] = formatted
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = formatted
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = formatted

    if latitude is not None and longitude is not None:
        exif_dict["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef: "N" if latitude >= 0 else "S",
            piexif.GPSIFD.GPSLatitude: _decimal_to_dms_rational(abs(latitude)),
            piexif.GPSIFD.GPSLongitudeRef: "E" if longitude >= 0 else "W",
            piexif.GPSIFD.GPSLongitude: _decimal_to_dms_rational(abs(longitude)),
        }

    try:
        exif_bytes = piexif.dump(exif_dict)
        output = io.BytesIO()
        piexif.insert(exif_bytes, content, output)
        return output.getvalue()
    except Exception:
        return content
