"""Staging ESA WorldCover at 80 m, and the mosaic written without gdalbuildvrt."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from basemap.pipeline import worldcover as wc


def test_each_staged_level_is_an_overview_of_the_map():
    assert wc.level_deg(2) == pytest.approx(wc.NATIVE_DEG * 8)
    assert wc.level_deg(4) == pytest.approx(wc.NATIVE_DEG * 32)
    # 74 m at the equator: two cover cells to a z12 pixel there, one at 60 N.
    assert wc.level_deg(2) * 111320 == pytest.approx(74.0, abs=0.5)
    assert [o for o, _ in wc.LEVELS] == [2, 4, 5]


def _tile(path, west, south, value, size=10, res=0.1):
    data = np.full((size, size), value, np.uint8)
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
                       dtype="uint8", crs="EPSG:4326", nodata=0, tiled=False,
                       transform=from_bounds(west, south, west + size * res,
                                             south + size * res, size, size)) as ds:
        ds.write(data, 1)
    return path


def test_the_mosaic_places_adjacent_tiles_on_one_grid(tmp_path):
    a = _tile(tmp_path / "A.tif", 0.0, 0.0, 10)
    b = _tile(tmp_path / "B.tif", 1.0, 0.0, 40)
    out = wc.write_vrt([a, b], tmp_path / "cover.vrt")
    text = out.read_text()
    assert 'relativeToVRT="1"' in text and "A.tif" in text and "/tmp" not in text
    with rasterio.open(out) as ds:
        assert (ds.width, ds.height) == (20, 10)
        assert ds.nodata == 0
        data = ds.read(1)
    assert (data[:, :10] == 10).all() and (data[:, 10:] == 40).all()


def test_the_mosaic_leaves_gaps_as_nodata(tmp_path):
    a = _tile(tmp_path / "A.tif", 0.0, 0.0, 10)
    c = _tile(tmp_path / "C.tif", 2.0, 0.0, 30)
    with rasterio.open(wc.write_vrt([a, c], tmp_path / "cover.vrt")) as ds:
        data = ds.read(1)
    assert data.shape == (10, 30)
    assert (data[:, 10:20] == 0).all()


def test_a_tile_off_the_grid_is_refused(tmp_path):
    a = _tile(tmp_path / "A.tif", 0.0, 0.0, 10)
    b = _tile(tmp_path / "B.tif", 1.0, 0.0, 40, res=0.05)
    with pytest.raises(SystemExit, match="grid"):
        wc.write_vrt([a, b], tmp_path / "cover.vrt")


def test_tile_names_come_from_the_grid_and_honour_the_box(tmp_path, monkeypatch):
    import fiona

    grid = tmp_path / "esa_worldcover_grid.fgb"
    schema = {"geometry": "Polygon", "properties": {"ll_tile": "str"}}
    with fiona.open(grid, "w", driver="FlatGeobuf", schema=schema, crs="EPSG:4326") as dst:
        for name, (w, s) in {"N42E003": (3, 42), "N45E003": (3, 45), "S03W060": (-60, -3)}.items():
            ring = [(w, s), (w + 3, s), (w + 3, s + 3), (w, s + 3), (w, s)]
            dst.write({"geometry": {"type": "Polygon", "coordinates": [ring]},
                       "properties": {"ll_tile": name}})
    monkeypatch.setattr(wc, "download_file", lambda url, dest, **kw: grid)
    assert wc.tile_names(tmp_path) == ["N42E003", "N45E003", "S03W060"]
    assert wc.tile_names(tmp_path, (4.0, 43.0, 5.0, 44.0)) == ["N42E003"]


def test_the_source_carries_the_coarser_levels_as_a_pyramid(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "get_work_dir", lambda base: tmp_path)
    dest = wc.cover_dir(tmp_path)
    dest.mkdir(parents=True)
    assert wc.source(tmp_path) is None
    for _, suffix in wc.LEVELS:
        wc.vrt_path(dest, suffix).write_text("<VRTDataset/>")
    src = wc.source(tmp_path)
    assert src.uri.endswith("worldcover.vrt")
    assert [round(res / wc.NATIVE_DEG) for res, _ in src.pyramid] == [32, 64]
    assert src.valid[0] >= 1, "0 is no data, not a class"


def test_the_steps_are_registered():
    from basemap.cli import STEPS

    assert STEPS["worldcover"] == "basemap.pipeline.worldcover"
    assert STEPS["rivers"] == "basemap.pipeline.rivers"


def test_build_vrts_takes_each_level_alone(tmp_path):
    dest = tmp_path / "worldcover"
    dest.mkdir()
    _tile(dest / "N00E000.tif", 0.0, 0.0, 10)
    _tile(dest / "N00E000_ov1.tif", 0.0, 0.0, 10, size=5, res=0.2)
    _tile(dest / "N00E000_ov2.tif", 0.0, 0.0, 10, size=2, res=0.5)
    wc.build_vrts(dest)
    for suffix, width in (("", 10), ("_ov1", 5), ("_ov2", 2)):
        with rasterio.open(wc.vrt_path(dest, suffix)) as ds:
            assert ds.width == width
