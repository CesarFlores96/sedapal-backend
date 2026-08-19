import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.media_storage import supervision_media_dir, supervision_media_url
from app.routers.planillas import _parse_captured_at, _parse_planilla_date


class PlanillaMediaStorageTests(unittest.TestCase):
    def test_media_directory_uses_planilla_operational_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            with patch("app.media_storage.get_settings") as settings:
                settings.return_value.supervision_media_root = temporary_root

                target = supervision_media_dir(
                    "planilla-123",
                    storage_date=date(2026, 8, 4),
                )

        self.assertEqual(
            target,
            Path(temporary_root) / "Agosto_2026" / "04_08_2026",
        )

    def test_media_url_uses_same_planilla_date(self) -> None:
        url = supervision_media_url(
            "planilla-123",
            "3020982_1.jpg",
            storage_date=date(2026, 8, 11),
        )

        self.assertEqual(
            url,
            "/uploads/supervision-media/Agosto_2026/11_08_2026/3020982_1.jpg",
        )

    def test_planilla_date_accepts_database_date_and_iso_form_value(self) -> None:
        self.assertEqual(_parse_planilla_date(date(2026, 8, 5)), date(2026, 8, 5))
        self.assertEqual(_parse_planilla_date("2026-08-12"), date(2026, 8, 12))

    def test_capture_timestamp_remains_available_for_photo_metadata(self) -> None:
        captured_at = _parse_captured_at("2026-08-12T15:30:00.000Z")

        self.assertIsNotNone(captured_at)
        self.assertEqual(captured_at.date(), date(2026, 8, 12))


if __name__ == "__main__":
    unittest.main()
