import unittest

from app.routers.navigation import normalize_osrm_route

class NavigationRouteTests(unittest.TestCase):
    def test_normalize_osrm_route_exposes_geometry_and_maneuvers(self) -> None:
        result = normalize_osrm_route({
            "code": "Ok",
            "routes": [{
                "distance": 1200.4,
                "duration": 180.2,
                "geometry": {"coordinates": [[-77.0, -12.0], [-77.01, -12.01]]},
                "legs": [{
                    "distance": 1200.4,
                    "duration": 180.2,
                    "steps": [{
                        "distance": 80.5,
                        "duration": 12.7,
                        "name": "Av. Arequipa",
                        "maneuver": {"type": "turn", "modifier": "right", "location": [-77.0, -12.0]},
                    }],
                }],
            }],
        })

        self.assertEqual(result["source"], "osrm")
        self.assertEqual(result["distanceMeters"], 1200)
        self.assertEqual(result["legs"][0]["steps"][0]["instruction"], "Gira a la derecha por Av. Arequipa")

    def test_normalize_osrm_route_rejects_missing_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "ruta transitable"):
            normalize_osrm_route({"code": "Ok", "routes": [{}]})
