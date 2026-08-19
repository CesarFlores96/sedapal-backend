from pathlib import Path

from app.media_storage import sanitize_folder_segment

UPLOADS_ROOT = Path(__file__).resolve().parent / "uploads" / "ota"


def ota_bundle_dir(channel: str, runtime_version: str, update_id: str) -> Path:
    target_dir = (
        UPLOADS_ROOT
        / sanitize_folder_segment(channel)
        / sanitize_folder_segment(runtime_version)
        / update_id
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def ota_bundle_file(channel: str, runtime_version: str, update_id: str, relative_path: str) -> Path:
    """Resuelve un archivo dentro del bundle ya publicado, sin crear directorios.

    ``relative_path`` puede traer separadores ``/`` (rutas dentro de ``dist/``,
    ej. ``_expo/static/js/android/entry-<hash>.hbc``); se valida que el resultado
    no escape de la carpeta del update (sin ``..``).
    """

    base_dir = (
        UPLOADS_ROOT
        / sanitize_folder_segment(channel)
        / sanitize_folder_segment(runtime_version)
        / update_id
    ).resolve()
    candidate = (base_dir / relative_path).resolve()
    if base_dir not in candidate.parents and candidate != base_dir:
        raise ValueError("Ruta de asset invalida.")
    return candidate
