import unittest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import Settings


class CorsConfigTests(unittest.TestCase):
    def test_packaged_tauri_origin_is_allowed(self) -> None:
        settings = Settings(
            DATABASE_URL="postgresql://test:test@localhost/test",
            API_KEY="test",
            _env_file=None,
        )
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins_list,
            allow_origin_regex=settings.allowed_origin_regex_value,
        )

        @app.get("/tile")
        def tile() -> dict[str, bool]:
            return {"ok": True}

        response = TestClient(app).get("/tile", headers={"Origin": "http://tauri.localhost"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://tauri.localhost")


if __name__ == "__main__":
    unittest.main()
