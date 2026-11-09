"""The land-cover tint and the shoreline features on the land relief."""

from __future__ import annotations

import numpy as np
import pytest

from basemap.processing import landcover as lc


def _lum(rgb):
    return (rgb[..., 0] * 54 + rgb[..., 1] * 183 + rgb[..., 2] * 18) / 256


def test_built_up_land_takes_the_colour_of_the_land_around_it():
    """Towns are neutral: at any strength they drew Padova and Faro as grey smudges."""
    rgb = np.full((4, 4, 3), 210.0)
    assert lc.tint(rgb, np.full((4, 4), 50, np.uint8)) == pytest.approx(rgb)
    assert lc.TINT[50] == (1.0, 0, 0.0)


def test_trees_are_darker_than_grass_and_crops_lighter():
    rgb = np.full((1, 3, 3), 210.0)
    cover = np.array([[10, 30, 40]], np.uint8)
    out = lc.tint(rgb, cover)
    tree, grass, crop = _lum(out[0])
    assert tree < grass < crop
    # And on the blue-yellow axis: trees bluer, crops warmer.
    assert out[0, 0, 2] - out[0, 0, 0] > out[0, 2, 2] - out[0, 2, 0]


def test_nothing_is_tinted_where_there_is_no_cover():
    rgb = np.full((2, 2, 3), 180.0)
    assert lc.tint(rgb, np.zeros((2, 2), np.uint8)) == pytest.approx(rgb)


def test_flats_move_a_quarter_of_the_way_at_the_published_strength():
    """Seasonally wet ground is painted toward grey-blue, at a quarter strength."""
    rgb = np.full((1, 3, 3), 210.0)
    occurrence = np.array([[0.0, 40.0, 80.0]])
    out = lc.features(rgb, np.zeros((1, 3), np.uint8), occurrence)
    assert out[0, 0] == pytest.approx([210.0] * 3), "dry land is untouched"
    assert out[0, 2] == pytest.approx([210.0] * 3), "and so is the water side"
    share = 0.5 * lc.FEATURE_STRENGTH
    expected = np.array([210.0] * 3) * (1 - share) + np.array(lc.FLATS_RGB) * share
    assert out[0, 1] == pytest.approx(expected)
    assert lc.FEATURE_STRENGTH == 0.25


def test_the_flats_band_ends_where_coastal_water_begins():
    from basemap.pipeline import xyz

    assert lc.FLATS_OCCURRENCE[1] == xyz.COASTAL_WATER_THRESHOLD


def test_wetland_goes_bluer_and_sand_warmer():
    rgb = np.full((1, 2, 3), 200.0)
    out = lc.features(rgb, np.array([[90, 60]], np.uint8), np.zeros((1, 2)))
    assert out[0, 0, 2] - out[0, 0, 0] > 0, "marsh: blue over red"
    assert out[0, 1, 0] - out[0, 1, 2] > 0, "sand: red over blue"


def test_zero_strength_paints_no_features():
    rgb = np.full((1, 2, 3), 200.0)
    out = lc.features(rgb, np.array([[90, 60]], np.uint8), np.full((1, 2), 40.0), strength=0)
    assert out == pytest.approx(rgb)


def _write_cover(path, west, south, east, north, value, size=64):
    import rasterio
    from rasterio.transform import from_bounds

    data = np.full((size, size), value, np.uint8)
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
                       dtype="uint8", crs="EPSG:4326", nodata=0,
                       transform=from_bounds(west, south, east, north, size, size)) as ds:
        ds.write(data, 1)


def test_read_cover_is_nearest_neighbour_and_zero_off_the_source(tmp_path):
    from basemap.processing.raster_tiles import MERCATOR, Source

    z, x, y = 10, *MERCATOR.lonlat_to_tile(4.7, 43.5, 10)
    w, s, e, n = MERCATOR.tile_lonlat_bounds(z, x, y)
    path = tmp_path / "cover.tif"
    _write_cover(path, w - 0.1, s - 0.1, e + 0.1, n + 0.1, 40)
    source = Source(str(path), bounds=(w - 0.1, s - 0.1, e + 0.1, n + 0.1))
    cover = lc.read_cover(source, z, x, y)
    assert cover.dtype == np.uint8 and cover.shape == (256, 256)
    assert (cover == 40).all()
    assert not lc.read_cover(source, z, x + 50, y).any()


def test_apply_without_water_tints_but_paints_no_flats(tmp_path):
    from basemap.processing.raster_tiles import MERCATOR, Source

    z, x, y = 10, *MERCATOR.lonlat_to_tile(4.7, 43.5, 10)
    w, s, e, n = MERCATOR.tile_lonlat_bounds(z, x, y)
    path = tmp_path / "cover.tif"
    _write_cover(path, w - 0.1, s - 0.1, e + 0.1, n + 0.1, 10)
    rgb = np.full((256, 256, 3), 210.0)
    out = lc.LandCover(cover=Source(str(path))).apply(rgb, z, x, y)
    assert out == pytest.approx(lc.tint(rgb, np.full((256, 256), 10, np.uint8)))


def test_the_land_step_refuses_to_render_without_the_cover(monkeypatch, tmp_path):
    from basemap.pipeline import hypsometric

    monkeypatch.delenv("BASEMAP_LAND_COVER", raising=False)
    monkeypatch.setattr(hypsometric.worldcover, "source", lambda base: None)
    with pytest.raises(SystemExit, match="worldcover"):
        hypsometric.land_cover_for(tmp_path)


def test_the_tint_can_be_switched_off_on_purpose(monkeypatch, tmp_path):
    from basemap.pipeline import hypsometric

    monkeypatch.setenv("BASEMAP_LAND_COVER", "off")
    assert hypsometric.land_cover_for(tmp_path) is None


def test_the_render_applies_the_cover_after_the_light():
    """Tint after shading, as judged: the light must not scale the colour shifts."""
    import inspect

    from basemap.processing import raster_tiles

    source = inspect.getsource(raster_tiles._paint)
    shaded = source.index("rgb = shade(rgb, light, shading)")
    assert shaded < source.index("spec.land_cover.apply(")
