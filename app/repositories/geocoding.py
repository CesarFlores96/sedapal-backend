import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request


def is_valid_coordinate_pair(latitude: float, longitude: float) -> bool:
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def coordinates_from_pair(latitude_text: str, longitude_text: str) -> tuple[float, float] | None:
    latitude = float(latitude_text)
    longitude = float(longitude_text)
    if not is_valid_coordinate_pair(latitude, longitude):
        return None
    return (longitude, latitude)


def decode_coordinate_text(text: str) -> str:
    try:
        return urllib.parse.unquote(text)
    except Exception:
        return text


def parse_coordinates_from_text(text: str) -> tuple[float, float] | None:
    decoded_text = decode_coordinate_text(text)
    at_coordinates = re.search(r"@(-?\d{1,2}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)(?:[,/?]|$)", decoded_text)
    if at_coordinates:
        return coordinates_from_pair(at_coordinates.group(1), at_coordinates.group(2))

    data_coordinates = re.search(r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)", decoded_text)
    if data_coordinates:
        return coordinates_from_pair(data_coordinates.group(1), data_coordinates.group(2))

    try:
        parsed = urllib.parse.urlparse(decoded_text)
        query_params = urllib.parse.parse_qs(parsed.query)
        for param in ("q", "query", "destination", "origin", "ll", "center"):
            value = query_params.get(param, [None])[0]
            if value:
                coordinates = parse_coordinates_from_text(value)
                if coordinates:
                    return coordinates
    except Exception:
        pass

    plain_coordinates = re.search(
        r"(?:^|[^\d.-])(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)(?:$|[^\d.])",
        decoded_text,
    )
    if plain_coordinates:
        return coordinates_from_pair(plain_coordinates.group(1), plain_coordinates.group(2))

    return None


def _load_url_text(url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
        body = response.read().decode("utf-8", errors="ignore")
    return final_url, body


async def search_address(query: str) -> dict[str, float] | None:
    url = (
        "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=pe&q="
        + urllib.parse.quote(query)
    )

    try:
        _final_url, body = await asyncio.to_thread(
            _load_url_text,
            url,
            {
                "Accept-Language": "es-PE,es;q=0.9",
                "User-Agent": "Sedapal FastAPI Migration/1.0",
            },
        )
        results = json.loads(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    if not results:
        return None

    first = results[0]
    return {"lat": float(first["lat"]), "lon": float(first["lon"])}


async def resolve_map_link(map_input: str) -> tuple[float, float] | None:
    direct_coordinates = parse_coordinates_from_text(map_input)
    if direct_coordinates:
        return direct_coordinates

    normalized_url = map_input if re.match(r"^https?://", map_input, re.IGNORECASE) else f"https://{map_input}"

    try:
        final_url, html = await asyncio.to_thread(
            _load_url_text,
            normalized_url,
            {"User-Agent": "Sedapal FastAPI Migration/1.0"},
        )
    except urllib.error.URLError:
        return None

    final_coordinates = parse_coordinates_from_text(final_url)
    if final_coordinates:
        return final_coordinates

    return parse_coordinates_from_text(html)
