"""BlueTopo staging: what gets kept, and what the renderer is handed."""

from __future__ import annotations

import inspect

import pytest

from basemap.pipeline import bluetopo


def test_only_surveyed_pixels_are_kept():
    """Most of BlueTopo below 200 m is GMRT fill, not survey.

    It is coarse and triangulated, and it rendered as visible facets in the
    Monterey canyon -- 96% of one offshore tile. The attribute table grades a
    survey 1 and the fill 3, so the fill is masked and those pixels fall
    through to GEBCO, which is where they came from and is honest about its
    resolution. The test is "keep 1", not "drop 3", so a grade nobody has seen
    yet cannot slip in as survey.
    """
    assert bluetopo.SURVEYED == 1
    source = inspect.getsource(bluetopo.unsurveyed)
    assert "!= SURVEYED" in source


def test_a_tile_that_is_all_fill_is_not_written():
    """It would put a source in the stack that can never supply a pixel.

    hr_seed_tiles plants a z12 seed over every primary source's rectangle, so
    an empty grid costs seeds the renderer then finds nothing in. Two of the
    first five tiles staged were all fill.
    """
    source = inspect.getsource(bluetopo.stage_one)
    assert "if not kept:" in source
    assert source.index("if not kept:") < source.index("rio_copy")


def test_every_region_claims_its_tiles_and_no_others():
    """CONUS first, Alaska and Puerto Rico later, and no tile in two regions.

    The UTM zones straddle them -- zone 19 carries both Maine and Puerto Rico
    -- so the split is by bounding box. Measured against the published tile
    scheme the three boxes take 6,245, 2,209 and 235 of the 8,689 published
    tiles, which is all of them; the points below are real tile centres from
    each, so a box that drifts drops or steals one.
    """
    from shapely.geometry import box

    boxes = {name: box(*b) for name, b in bluetopo.REGIONS.items()}
    names = sorted(boxes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            overlap = boxes[a].intersection(boxes[b]).area
            assert overlap == 0, f"{a} and {b} overlap by {overlap:.2f} sq deg"

    from shapely.geometry import Point

    places = {
        "conus": [(-122.06, 36.79), (-70.5, 42.3), (-89.0, 29.0), (-117.3, 32.7)],
        "alaska": [(-134.5, 57.5), (-130.2, 55.4)],
        "prusvi": [(-66.1, 18.4), (-64.8, 18.3)],
    }
    for region, points in places.items():
        for lon, lat in points:
            inside = [n for n, b in boxes.items() if b.covers(Point(lon, lat))]
            assert inside == [region], f"({lon}, {lat}) landed in {inside}, wanted {region}"


def test_the_renderer_is_handed_bluetopo_without_sign_probing():
    """EMODnet's grids need their sign measured; BlueTopo does not.

    It stores elevation negative-down like GEBCO -- checked against the files,
    which read -1499 to -52 m over Monterey -- and carries a compound CRS with
    NAVD88, an orthometric datum, so it needs no chart-datum conversion either.
    """
    from basemap.pipeline import bathymetry

    source = inspect.getsource(bathymetry.bluetopo_sources)
    assert "grid_sign" not in source, "BlueTopo should not be sign-probed"
    assert "transform_bounds" in source, "bounds must reach WGS84 for tile seeding"
    assert "bluetopo_sources(work)" in inspect.getsource(bathymetry.specs)


def test_a_render_box_narrows_the_hr_seeds():
    """A new HR source is judged on a box before the whole footprint.

    The CONUS grids seed some forty-six thousand z12 tiles and descend to z15;
    California alone is a fraction of that. Without this the only way to look
    at a new source was to render everything it covers.
    """
    import inspect

    from basemap.pipeline import bathymetry

    source = inspect.getsource(bathymetry.hr_seed_tiles)
    assert "render_bbox" in source
    assert "seeds &= inside" in source, "the box must intersect, not replace"
