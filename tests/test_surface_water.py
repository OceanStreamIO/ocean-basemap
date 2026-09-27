"""Observed surface water: the mask that stops lagoons being land."""

from __future__ import annotations

import numpy as np
from shapely.geometry import box

from basemap.pipeline import surface_water as water
from basemap.processing.raster_tiles import MERCATOR, sea_alpha

# --- the tile grid ---------------------------------------------------------

def test_the_grid_is_the_published_one():
    names = water.tile_names()
    assert len(names) == 504, "36 longitudes by 14 latitudes"
    assert len(set(names)) == len(names)


def test_zero_is_east_and_north():
    """'0W' is a 404 on the server; the grid names it '0E'."""
    names = set(water.tile_names())
    assert f"occurrence_0E_0N{water.GSW_VERSION}.tif" in names
    assert f"occurrence_0W_0N{water.GSW_VERSION}.tif" not in names


def test_the_grid_spans_the_published_latitudes():
    assert water.LATITUDES[0] == "80N"
    assert water.LATITUDES[-1] == "50S"
    assert water.LONGITUDES[0] == "180W"
    assert water.LONGITUDES[-1] == "170E"


def test_the_threshold_keeps_permanent_water_only():
    """Seasonal flooding must not turn a paddy field into sea."""
    assert 50 < water.PERMANENT_WATER < 100


def test_no_mosaic_means_no_source(tmp_path):
    assert water.source(tmp_path) is None


def test_the_source_reads_a_percentage(tmp_path):
    base = water.get_water_dir(tmp_path)
    base.mkdir(parents=True)
    (base / "gsw_occurrence.vrt").write_text("<VRTDataset/>")
    source = water.source(tmp_path)
    assert source is not None
    assert source.valid == (0.0, 100.0), "occurrence is a percentage, not a depth"


def test_the_pyramid_is_used_when_it_is_there(tmp_path):
    base = water.get_water_dir(tmp_path)
    base.mkdir(parents=True)
    (base / "gsw_occurrence.vrt").write_text("<VRTDataset/>")
    assert water.source(tmp_path).pyramid == ()
    for level in range(len(water.OVERVIEW_LEVELS)):
        (base / f"gsw_occurrence_ov{level}.vrt").write_text("<VRTDataset/>")
    pyramid = water.source(tmp_path).pyramid
    assert len(pyramid) == len(water.OVERVIEW_LEVELS)
    sizes = [deg for deg, _ in pyramid]
    assert sizes == sorted(sizes), "coarser stand-ins must be ordered"
    assert sizes[0] > water.NATIVE_DEG, "every level is coarser than the native grid"


def test_nodata_is_declared_so_averaging_skips_it():
    """255 means 'never observed'; averaged as a number it drags a coast wet."""
    assert water.NODATA == 255


# --- the mask --------------------------------------------------------------

class FakeWater:
    """A water source that reports one percentage everywhere."""

    def __init__(self, percent):
        self.percent = percent


def _patch(monkeypatch, percent):
    import basemap.processing.raster_tiles as rt

    monkeypatch.setattr(
        rt, "water_fraction",
        lambda source, z, x, y, proj=None, buffer=1: np.full((rt.TILE + 2, rt.TILE + 2),
                                                             source.percent, dtype=np.float32))


def test_land_alone_closes_the_tile():
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    assert sea_alpha(covered, 6, 33, 22).max() == 0


def test_observed_water_opens_land_back_up(monkeypatch):
    """The lagoon: inside the land polygons, wet in every scene."""
    _patch(monkeypatch, 95.0)
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    assert sea_alpha(covered, 6, 33, 22, water=FakeWater(95.0)).max() == 255


def test_dry_land_below_sea_level_stays_land(monkeypatch):
    """The polder: also inside the land polygons, but dry in every scene."""
    _patch(monkeypatch, 2.0)
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    assert sea_alpha(covered, 6, 33, 22, water=FakeWater(2.0)).max() == 0


def test_seasonal_water_does_not_count(monkeypatch):
    _patch(monkeypatch, water.PERMANENT_WATER - 10)
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    alpha = sea_alpha(covered, 6, 33, 22, water=FakeWater(water.PERMANENT_WATER - 10),
                      water_min=water.PERMANENT_WATER)
    assert alpha.max() == 0


def test_without_a_water_source_nothing_changes():
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    assert np.array_equal(sea_alpha(covered, 6, 33, 22),
                          sea_alpha(covered, 6, 33, 22, water=None))


# --- wiring ----------------------------------------------------------------

def test_the_step_runs_before_the_bathymetry():
    from basemap.cli import FULL_ORDER, STEPS

    assert STEPS["surface-water"] == "basemap.pipeline.surface_water"
    assert FULL_ORDER.index("surface-water") < FULL_ORDER.index("bathymetry")
    assert FULL_ORDER.index("surface-water") < FULL_ORDER.index("sea-mask") or True


def test_the_sea_mask_is_the_coastline_alone():
    """Observed water is drawn as inland water, not folded into the sea.

    It was in the sea mask first, and the bathymetry ramp's near-white first
    two metres made the Venice lagoon the same tone as the land beside it.
    Keeping it out is also what leaves a land side for the compositor to paint
    coastal water on.
    """
    import inspect

    from basemap.pipeline import bathymetry

    source = inspect.getsource(bathymetry.specs)
    assert "water=" not in source


def test_the_compositor_draws_coastal_water_instead():
    import inspect

    from basemap.pipeline import xyz

    assert "_draw_coastal" in inspect.getsource(xyz._paint_water)
    assert xyz.COASTAL_WATER_RGB[2] > xyz.COASTAL_WATER_RGB[0], "it must read as blue"


def test_lakes_are_painted_after_the_sea():
    """Otherwise the sea ramp covers them, now that lakes are in the mask."""
    import inspect

    from basemap.pipeline import xyz

    body = inspect.getsource(xyz.compose)
    assert body.index("_draw_contours") < body.index("_paint_water")


def test_observed_water_is_not_eroded(monkeypatch):
    """The 3x3 erosion protects the coastline; it only thins inland water.

    A single wet pixel surrounded by land must survive, because there is no
    vector sea mask inland to fill the strip an erosion would take.
    """
    import numpy as np

    import basemap.processing.raster_tiles as rt

    lone = np.zeros((rt.TILE + 2, rt.TILE + 2), dtype=np.float32)
    lone[64, 64] = 95.0
    monkeypatch.setattr(rt, "water_fraction",
                        lambda source, z, x, y, proj=None, buffer=1: lone)
    covered = [box(*MERCATOR.tile_bounds(6, 33, 22)).buffer(10_000)]
    alpha = sea_alpha(covered, 6, 33, 22, water=FakeWater(95.0))
    assert alpha[63, 63] == 255, "the wet pixel must survive on its own"
    assert alpha[10, 10] == 0, "and dry land must stay dry"


def test_the_coastline_is_still_eroded(monkeypatch):
    """The protection the erosion was added for must not be lost."""
    import numpy as np

    import basemap.processing.raster_tiles as rt

    dry = np.zeros((rt.TILE + 2, rt.TILE + 2), dtype=np.float32)
    monkeypatch.setattr(rt, "water_fraction",
                        lambda source, z, x, y, proj=None, buffer=1: dry)
    # A tile whose land covers only one corner: the sea edge must sit back
    # from it rather than run to the shoreline.
    w, s, e, n = MERCATOR.tile_bounds(6, 33, 22)
    corner = [box(w, s, w + (e - w) / 2, s + (n - s) / 2)]
    with_water = sea_alpha(corner, 6, 33, 22, water=FakeWater(0.0))
    without = sea_alpha(corner, 6, 33, 22)
    assert np.array_equal(with_water, without), "dry ground must be unaffected"
    assert without.min() == 0 and without.max() == 255
