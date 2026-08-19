import pytest

from app.sedapalgis.repositories import gis


@pytest.mark.asyncio
async def test_lot_tile_inherits_block_and_lot_corrections(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_one(pool, query: str, params: tuple[int, ...]):
        captured.update(pool=pool, query=query, params=params)
        return {"tile": memoryview(b"mvt")}

    monkeypatch.setattr(gis, "fetch_one", fake_fetch_one)

    payload = await gis.fetch_corrected_lot_tile(object(), 17, 37483, 69977)

    assert payload == b"mvt"
    assert captured["params"] == (17, 37483, 69977, 17, gis.MAX_CORRECTION_DEGREES)
    query = str(captured["query"])
    assert "FROM public.gis_lots l" in query
    assert "FROM gis.lots l" not in query
    assert "ST_Transform(bounds.geom_3857, 4326)" in query
    assert "block_correction.block_id = l.block_id" in query
    assert "lot_correction.lot_id = l.id" in query
    assert "COALESCE(block_correction.delta_lng, 0) + COALESCE(lot_correction.delta_lng, 0)" in query
    assert "COALESCE(block_correction.delta_lat, 0) + COALESCE(lot_correction.delta_lat, 0)" in query
