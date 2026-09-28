"""Mosaics must not name the machine that built them.

The DEM and surface-water mosaics list thousands of tiles. With absolute paths
they only work on the filesystem that built them, which breaks the moment the
same tree is read from a cluster's shared storage or copied to a compute node's
local disk -- and it breaks by returning no data rather than by failing.
"""

from __future__ import annotations

import shutil

import pytest

from basemap.pipeline import surface_water


def _has_gdalbuildvrt() -> bool:
    return shutil.which("gdalbuildvrt") is not None


pytestmark = pytest.mark.skipif(not _has_gdalbuildvrt(), reason="needs gdalbuildvrt")


def _tile(path, value, west, size=64, overviews=(2, 4, 8)):
    """A 1-degree byte GeoTIFF at *west*, filled with *value*.

    Carries overviews because both mosaics build one VRT per COG overview level
    (``-oo OVERVIEW_LEVEL=n``), which is how they read a coarse zoom without
    touching full-resolution pixels. Real Copernicus and GSW tiles have them.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    data = np.full((size, size), value, dtype="uint8")
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_origin(west, 1.0, 1.0 / size, 1.0 / size),
    ) as ds:
        ds.write(data, 1)
        ds.build_overviews(list(overviews), Resampling.average)
    return path


def test_the_surface_water_mosaic_is_relative_and_survives_a_move(tmp_path):
    src = tmp_path / "gsw"
    src.mkdir()
    tiles = [_tile(src / f"occurrence_{i}.tif", 10 + i, float(i)) for i in range(2)]

    vrt = surface_water.build_vrt(tiles, src)
    text = vrt.read_text()
    assert 'relativeToVRT="1"' in text
    assert str(tmp_path) not in text, "the mosaic still names an absolute path"

    # The move is the point: a relative mosaic reads its tiles wherever it is.
    moved = tmp_path / "elsewhere"
    shutil.move(str(src), str(moved))

    import rasterio

    with rasterio.open(moved / vrt.name) as ds:
        assert ds.read(1).max() == 11


def test_the_listing_holds_names_not_paths(tmp_path):
    src = tmp_path / "gsw"
    src.mkdir()
    tiles = [_tile(src / "occurrence_a.tif", 7, 0.0)]
    surface_water.build_vrt(tiles, src)
    assert (src / "gsw.list").read_text().strip() == "occurrence_a.tif"


def test_the_dem_mosaic_is_relative(tmp_path):
    """hypsometric.mosaics keeps its tiles in a product subdirectory."""
    from basemap.pipeline import hypsometric

    dem_dir = tmp_path / "land_dem"
    (dem_dir / "glo90").mkdir(parents=True)
    for i in range(2):
        _tile(dem_dir / "glo90" / f"Copernicus_DSM_{i}_DEM.tif", 20 + i, float(i))

    source = hypsometric.mosaics(dem_dir, "glo90")
    text = (dem_dir / "glo90.vrt").read_text()
    assert 'relativeToVRT="1"' in text
    assert str(tmp_path) not in text
    assert (dem_dir / "glo90.list").read_text().splitlines()[0].startswith("glo90/")
    assert source.uri.endswith("glo90.vrt")
    assert len(source.pyramid) == 2, "one stand-in per COG overview level"


def test_gdalbuildvrt_is_invoked_from_the_mosaic_directory():
    """Relative paths are only relative if cwd is where the VRT lands."""
    import inspect

    for func in (surface_water.build_vrt,):
        body = inspect.getsource(func)
        assert "cwd=dest" in body, "build_vrt must run gdalbuildvrt in the mosaic directory"
    body = inspect.getsource(__import__(
        "basemap.pipeline.hypsometric", fromlist=["mosaics"]).mosaics)
    assert "cwd=dem_dir" in body
