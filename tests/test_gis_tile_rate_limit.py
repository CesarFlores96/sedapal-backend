import unittest
from unittest.mock import patch

from starlette.requests import Request

from app import main


class GisTileRateLimitTests(unittest.TestCase):
    def test_signed_tiles_use_the_high_throughput_rate_limit(self) -> None:
        sentinel = object()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/gis/tiles/signed-token/mvt.lots/15/123/456",
                "headers": [],
                "client": ("127.0.0.1", 54321),
            }
        )

        with patch.object(main.rate_limit, "check", return_value=sentinel) as check:
            self.assertIs(main._resolve_rate_limit(request), sentinel)
            check.assert_called_once_with(
                "tiles:127.0.0.1",
                main.settings.rate_limit_trusted_max,
                main.settings.rate_limit_trusted_window,
            )


if __name__ == "__main__":
    unittest.main()
