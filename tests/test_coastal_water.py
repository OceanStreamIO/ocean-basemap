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


def test_deep_bodies_take_the_sea_ramp_and_shallow_ones_the_lagoon_tone():
    """The Amazon's channels, -12 m over any 5 km window, match the sea outside
    the coastline; Venice, the Camargue and the limans at -2 keep the lagoon
    tone; between the two the colour blends so nothing has a hard edge."""
    import numpy as np

    from basemap.pipeline import xyz

    coverage = np.ones((4, 4))
    depth = np.array([[-20.0, -5.0, -2.0, 0.0]] * 4)
    layer = xyz.coastal_rgba(coverage, outline=False, depth=depth)
    sea = tuple(int(v) for v in xyz._sea_ramp().colour(np.array([-20.0]))[0].round())
    assert tuple(layer[0, 0, :3]) == sea
    assert tuple(layer[0, 2, :3]) == xyz.COASTAL_WATER_RGB
    assert tuple(layer[0, 3, :3]) == xyz.COASTAL_WATER_RGB
    mid = layer[0, 1, :3].astype(int)
    assert (mid != np.array(sea)).any(), "half way: a blend"
    assert (mid != np.array(xyz.COASTAL_WATER_RGB)).any()
    assert (layer[..., 3] == 255).all()
    plain = xyz.coastal_rgba(coverage, outline=False)
    assert (plain[..., :3] == xyz.COASTAL_WATER_RGB).all(), "without a depth nothing changes"
    shallow, deep = xyz.COASTAL_SEA_BLEND
    assert -4 <= shallow <= -2 and deep < shallow, "the lagoons measure -2, the Amazon -12"


def test_the_window_mean_ignores_land_and_never_sees_a_strip_edge():
    import numpy as np

    from basemap.pipeline import xyz

    values = np.array([[-10.0, 50.0, -10.0], [-10.0, 50.0, -10.0], [-10.0, 50.0, -10.0]])
    wet = values < 0
    mean = xyz._box_mean(values, wet, 1)
    assert mean[1, 1] == -10.0, "land in the window does not count"
    assert np.isnan(xyz._box_mean(values, np.zeros_like(wet), 1)).all()
    import inspect

    source = inspect.getsource(xyz.Water.depth)
    assert "pad=pad" in source and "[pad:-pad, pad:-pad]" in source


def test_the_outline_follows_the_pixel_it_sits_on():
    import numpy as np

    from basemap.pipeline import xyz

    coverage = np.zeros((8, 8))
    coverage[2:6, 2:6] = 1.0
    layer = xyz.coastal_rgba(coverage, outline=True, depth=np.full((8, 8), -30.0))
    sea = xyz._sea_ramp().colour(np.array([-30.0]))[0].round()
    r, g, b, a = xyz.LAKE_OUTLINE_RGBA
    expected = (sea * (1 - a / 255) + np.array([r, g, b]) * (a / 255)).round()
    assert tuple(layer[2, 2, :3]) == tuple(int(v) for v in expected)


def test_the_archive_and_the_tree_both_pass_the_elevation():
    import inspect

    from basemap.pipeline import coastal_water, xyz

    assert "depth=water.depth(z, x, y)" in inspect.getsource(coastal_water._render)
    assert "water.depth(z, x, y)" in inspect.getsource(xyz._paint_water)


def test_the_depth_window_is_sized_from_a_tile_row_not_a_strip_index():
    """At z12 over Venice a strip index read as a row put the window at 84 N,
    and the lagoon came out at -7 m instead of its -1.4."""
    import inspect

    from basemap.pipeline import xyz

    venice = xyz.Water.depth_radius_px(12, 2187, 1466)
    assert 85 <= venice <= 100, venice
    assert xyz.Water.depth_radius_px(12, 2187, 1466 >> 3) > 3 * venice, "what the bug did"
    source = inspect.getsource(xyz.Water.depth)
    assert "gy << self.GROUP" in source and "depth_radius_px(z, x, top)" in source
