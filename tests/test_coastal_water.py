"""Observed coastal water: the threshold, the edge, and one layer for both products."""

from __future__ import annotations

import inspect

import numpy as np

from basemap.pipeline import coastal_water, xyz


def test_seasonal_water_is_land_and_permanent_water_is_full():
    """The Camargue's rice fields and the Po plain's irrigation sit at 40-70%
    occurrence. Painted at half strength they were a grey-white veil over the
    whole delta; at the threshold they are land, as EMODnet draws them."""
    occurrence = np.array([[40.0, 60.0, 65.0, 70.0, 95.0, np.nan]], dtype=np.float32)
    coverage = xyz.coastal_coverage(occurrence)
    assert coverage[0, 0] == 0 and coverage[0, 1] == 0, "a paddy field is land"
    assert coverage[0, 2] == 0.5 and coverage[0, 3] == 1 and coverage[0, 4] == 1
    assert coverage[0, 5] == 0, "never observed is not water"
    assert 60 <= xyz.COASTAL_WATER_THRESHOLD <= 80


def test_the_layer_outlines_the_water_it_fills():
    coverage = np.zeros((16, 16), dtype=np.float32)
    coverage[4:12, 4:12] = 1.0
    layer = xyz.coastal_rgba(coverage, outline=True)
    assert tuple(layer[8, 8, :3]) == xyz.COASTAL_WATER_RGB and layer[8, 8, 3] == 255
    assert layer[1, 1, 3] == 0, "nothing outside the water"
    assert tuple(layer[4, 8, :3]) != xyz.COASTAL_WATER_RGB and layer[4, 8, 3] == 255, \
        "the edge carries the lakes' outline"
    plain = xyz.coastal_rgba(coverage, outline=False)
    assert tuple(plain[4, 8, :3]) == xyz.COASTAL_WATER_RGB


def test_no_line_is_drawn_where_a_lagoon_opens_into_the_sea():
    """The water continues through the inlet, so there is no edge there; the
    compositor keeps the sea side off with on_land, not with a line."""
    coverage = np.ones((16, 16), dtype=np.float32)        # lagoon and sea alike
    on_land = np.zeros((16, 16), dtype=np.float32)
    on_land[:, :8] = 1
    base = np.full((16, 16, 3), 100, dtype=np.uint8)
    out = xyz._draw_coastal(base, coverage, on_land, 12)
    assert tuple(out[8, 3]) == xyz.COASTAL_WATER_RGB, "painted on the land side"
    assert tuple(out[8, 12]) == (100, 100, 100), "left alone on the sea side"
    assert tuple(out[8, 7]) == xyz.COASTAL_WATER_RGB, "and no line where the water goes on"


def test_the_outline_waits_for_a_zoom_where_it_is_not_noise():
    coverage = np.zeros((16, 16), dtype=np.float32)
    coverage[4:12, 4:12] = 1.0
    base = np.full((16, 16, 3), 100, dtype=np.uint8)
    low = xyz._draw_coastal(base, coverage, None, xyz.COASTAL_OUTLINE_MIN_ZOOM - 1)
    high = xyz._draw_coastal(base, coverage, None, xyz.COASTAL_OUTLINE_MIN_ZOOM)
    assert tuple(low[4, 8]) == xyz.COASTAL_WATER_RGB
    assert tuple(high[4, 8]) != xyz.COASTAL_WATER_RGB


def test_the_archive_writes_the_layer_the_tree_paints():
    """Two products, one function: an outline added to one and not the other
    would be the viewer and the tree disagreeing about the same lagoon."""
    assert "coastal_rgba(" in inspect.getsource(coastal_water._render)
    assert "COASTAL_OUTLINE_MIN_ZOOM" in inspect.getsource(coastal_water._render)
    assert "coastal_rgba(" in inspect.getsource(xyz._draw_coastal)


def test_water_the_bathymetry_calls_sea_takes_the_sea_ramp():
    """Below COASTAL_SEA_FLOOR the layer is the sea's colour at that depth, so
    the Amazon's channels match the sea outside the coastline instead of
    being a lagoon twenty luminance levels darker. At or above it, the
    lagoon tone -- Venice, the Camargue, Ria Formosa -- stays."""
    import numpy as np

    from basemap.pipeline import xyz

    coverage = np.ones((4, 4))
    elevation = np.array([[-40.0, -40.0, 0.0, 0.0]] * 4)
    layer = xyz.coastal_rgba(coverage, outline=False, elevation=elevation)
    deep = tuple(layer[0, 0, :3])
    shallow = tuple(layer[0, 3, :3])
    assert shallow == xyz.COASTAL_WATER_RGB
    assert deep == tuple(int(v) for v in xyz._sea_ramp().colour(np.array([-40.0]))[0].round())
    assert deep != xyz.COASTAL_WATER_RGB
    assert (layer[..., 3] == 255).all()
    plain = xyz.coastal_rgba(coverage, outline=False)
    assert (plain[..., :3] == xyz.COASTAL_WATER_RGB).all(), "without elevation nothing changes"
    assert xyz.COASTAL_SEA_FLOOR < 0 > -5, "a couple of metres: GEBCO's noise on a mudflat is not sea"


def test_the_outline_follows_the_pixel_it_sits_on():
    import numpy as np

    from basemap.pipeline import xyz

    coverage = np.zeros((8, 8)); coverage[2:6, 2:6] = 1.0
    elevation = np.full((8, 8), -30.0)
    layer = xyz.coastal_rgba(coverage, outline=True, elevation=elevation)
    sea = xyz._sea_ramp().colour(np.array([-30.0]))[0].round()
    r, g, b, a = xyz.LAKE_OUTLINE_RGBA
    expected = (sea * (1 - a / 255) + np.array([r, g, b]) * (a / 255)).round()
    assert tuple(layer[2, 2, :3]) == tuple(int(v) for v in expected)


def test_the_archive_and_the_tree_both_pass_the_elevation():
    import inspect

    from basemap.pipeline import coastal_water, xyz

    assert "elevation=water.elevation(z, x, y)" in inspect.getsource(coastal_water._render)
    assert "water.elevation(z, x, y)" in inspect.getsource(xyz._paint_water)
