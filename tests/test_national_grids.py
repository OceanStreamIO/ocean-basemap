"""CUDEM, AusBathyTopo, the per-source zoom cap and the regional archive."""

from __future__ import annotations

import zipfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from basemap.pipeline import ausbathytopo, bathymetry, cudem
from basemap.processing import raster_tiles as rt

# --- the cap ----------------------------------------------------------------

@pytest.mark.parametrize("cell, lat, zoom", [
    (4.0, 37.0, 16),      # BlueTopo: native z15 and more, clipped by the archive at 15
    (30.0, -16.0, 13),    # AusBathyTopo
    (58.0, 50.0, 12),     # the coarsest EMODnet HR survey
    (100.0, 37.0, 11),    # GMRT at its coarsest
    (49.0, 37.0, 12),     # GMRT at Monterey
])
def test_a_grid_earns_one_level_past_its_native_zoom(cell, lat, zoom):
    assert rt.native_zoom(cell, lat) == zoom


def _grid(path, west, south, east, north, value, size=64):
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999,
                       transform=from_bounds(west, south, east, north, size, size)) as ds:
        ds.write(np.full((size, size), value, np.float32), 1)
    return str(path)


def test_past_its_cap_a_source_fills_but_no_longer_earns_a_tile(tmp_path):
    z, x, y = 13, *rt.lonlat_to_tile(146.05, -16.25, 13)
    w, s, e, n = rt.tile_lonlat_bounds(z, x, y)
    uri = _grid(tmp_path / "g.tif", w - 0.1, s - 0.1, e + 0.1, n + 0.1, -40.0)
    capped = rt.Source(uri, bounds=(w - 0.1, s - 0.1, e + 0.1, n + 0.1), max_zoom=12)
    free = rt.Source(uri, bounds=(w - 0.1, s - 0.1, e + 0.1, n + 0.1))
    dem, hit = rt.read_elevation((capped,), z, x, y, primary=1)
    assert not hit, "a capped source alone does not earn a z13 tile"
    assert np.nanmin(dem) == pytest.approx(-40.0), "but it still fills one"
    _, hit = rt.read_elevation((free,), z, x, y, primary=1)
    assert hit
    _, hit = rt.read_elevation((capped,), 12, x >> 1, y >> 1, primary=1)
    assert hit, "at its own zoom it earns the tile"


def test_every_hr_and_regional_source_carries_a_cap():
    import inspect

    for fn in (bathymetry.hr_sources, bathymetry.bluetopo_sources, bathymetry.gmrt_sources,
               cudem.sources, ausbathytopo.sources):
        assert "max_zoom=" in inspect.getsource(fn), fn.__name__


# --- the regional archive ------------------------------------------------------

def test_regional_tiles_are_listed_box_by_box_not_by_their_union(monkeypatch):
    monkeypatch.delenv("BASEMAP_RENDER_BBOX", raising=False)
    gebco = rt.Source("gebco")
    west = rt.Source("a", bounds=(-122.5, 36.5, -121.5, 37.5))
    east = rt.Source("b", bounds=(146.0, -17.0, 147.0, -16.0), max_zoom=10)
    ramp = rt.Ramp((0, 1), ((0, 0, 0), (1, 1, 1)))
    spec = rt.RenderSpec((rt.Band(9, 12, (west, east, gebco), primary=2),), ramp)
    tiles = bathymetry.gmrt_tiles(spec)
    union = sum(1 for _ in rt.tiles_in_bbox((-122.5, -17.0, 147.0, 37.5), 12))
    assert len(tiles) < 2000 < union, "the union rectangle is most of the world"
    assert max(z for z, *_ in tiles) == 12
    lons = [rt.tile_lonlat_bounds(z, x, y)[0] for z, x, y in tiles if z > 10]
    assert all(lon < 0 for lon in lons), "the capped box stops at its own zoom"


def test_the_national_grids_feed_both_archives():
    import inspect

    source = inspect.getsource(bathymetry.specs)
    assert "national = national_sources(work)" in source
    assert "regional = [*national, *gmrt]" in source
    assert "bluetopo_sources(work) + national" in source


# --- CUDEM -----------------------------------------------------------------------

def test_a_cudem_tile_is_placed_by_its_name():
    assert cudem.tile_bounds("ncei19_n36x75_w122x00_2023v1.tif") == (-122.0, 36.5, -121.75, 36.75)
    assert cudem.tile_bounds("readme.txt") is None


def test_cudem_lists_every_page_of_a_region(monkeypatch):
    pages = [
        b"<r><Contents><Key>dem/x/CA/ncei19_n37x00_w122x00_2023v1.tif</Key><Size>10</Size></Contents>"
        b"<NextContinuationToken>tok/1</NextContinuationToken></r>",
        b"<r><Contents><Key>dem/x/CA/ncei19_n36x75_w122x00_2023v1.tif</Key><Size>20</Size></Contents></r>",
    ]
    asked = []

    class Reply:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

    def fake(url, timeout):
        asked.append(url)
        return Reply(pages[len(asked) - 1])

    monkeypatch.setattr(cudem.urllib.request, "urlopen", fake)
    keys = cudem.list_keys("CA")
    assert [k for k, _ in keys] == ["dem/x/CA/ncei19_n37x00_w122x00_2023v1.tif",
                                     "dem/x/CA/ncei19_n36x75_w122x00_2023v1.tif"]
    assert "continuation-token=tok%2F1" in asked[1]


def test_staged_cudem_tiles_become_capped_sources(tmp_path):
    grid = cudem.grids_dir(tmp_path) / "CA"
    grid.mkdir(parents=True)
    _grid(grid / "ncei19_n36x75_w122x00_2023v1.tif", -122.0, 36.5, -121.75, 36.75, -100.0)
    (src,) = cudem.sources(tmp_path)
    assert src.bounds == (-122.0, 36.5, -121.75, 36.75)
    assert src.crs == cudem.HORIZONTAL_CRS
    assert src.max_zoom >= 15, "3 m cells reach the archive's deepest zoom"


def test_nothing_is_staged_unless_a_region_is_named(monkeypatch, tmp_path):
    monkeypatch.delenv("BASEMAP_CUDEM_REGIONS", raising=False)
    monkeypatch.delenv("BASEMAP_AUSBATHYTOPO", raising=False)
    assert cudem.wanted_regions() == [] and ausbathytopo.wanted() == []


# --- AusBathyTopo ------------------------------------------------------------------

def test_the_regions_with_a_hydrographic_notice_are_marked():
    assert set(ausbathytopo.DATASETS) == {"north", "torres", "gbr", "bass"}
    assert ausbathytopo.NOTICE == {"gbr", "bass"}


def test_an_unknown_region_is_refused(monkeypatch):
    monkeypatch.setenv("BASEMAP_AUSBATHYTOPO", "north tasman")
    with pytest.raises(SystemExit, match="tasman"):
        ausbathytopo.wanted()


def test_a_zip_is_unpacked_to_its_geotiffs_and_removed(tmp_path, monkeypatch):
    tif = _grid(tmp_path / "src.tif", 121.0, -18.0, 124.0, -8.0, -30.0)
    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.write(tif, "North_Australia_A_cog.tif")
            zf.writestr("readme.txt", "x")
        return dest
    monkeypatch.setattr(ausbathytopo, "download_file", fake_download)
    grids = ausbathytopo.stage_one("north", tmp_path)
    assert [g.name for g in grids] == ["North_Australia_A_cog.tif"]
    assert not list((tmp_path / "ausbathytopo").glob("*.zip"))
    (src,) = ausbathytopo.sources(tmp_path)
    assert src.bounds == pytest.approx((121.0, -18.0, 124.0, -8.0))
    assert src.max_zoom == rt.native_zoom(3.0 / 64 * 111320.0, -13.0), "capped by its own cell"
    assert ausbathytopo.stage_one("north", tmp_path) == grids, "staged once"


def test_the_new_steps_are_registered_and_need_no_gdal_cli():
    from basemap.cli import STEPS

    assert STEPS["cudem"] == "basemap.pipeline.cudem"
    assert STEPS["ausbathytopo"] == "basemap.pipeline.ausbathytopo"
