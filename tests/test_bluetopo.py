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
    source = inspect.getsource(bluetopo.unusable)
    assert "== SURVEYED" in source


def test_a_grid_that_cannot_carry_its_area_is_not_written():
    """Partial coverage draws a ribbon, which reads as broken, not detailed.

    hr_seed_tiles plants a z12 seed over every primary source's rectangle. A
    grid that supplies a pixel in only part of that rectangle puts 4 m detail
    with a hard edge across an otherwise 460 m view. Coverage is bimodal
    rather than even -- across the 6,237 CONUS tiles, 36% are under a quarter
    covered and 39% over three quarters -- so there is a clean line to draw.
    """
    source = inspect.getsource(bluetopo.stage_one)
    assert "MIN_COVERAGE" in source
    assert source.index("MIN_COVERAGE") < source.index("rio_copy")
    assert 0.5 <= bluetopo.MIN_COVERAGE <= 0.9


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


def test_a_survey_too_old_to_grid_at_four_metres_is_dropped():
    """The assessment grade does not catch sparse old surveys.

    A 1935 lead-line survey grades 1, the same as 2013 multibeam, because the
    grade is about vertical accuracy and those soundings are accurate. What
    they are not is dense -- hundreds of metres apart, gridded onto 4 m -- and
    interpolating them drew speckle and straight track lines across the seabed
    off Monterey in the first California render. Nor does the uncertainty
    column separate them: 2 m against 1985's 1 m, which is again accuracy.
    """
    source = inspect.getsource(bluetopo.unusable)
    assert "MIN_SURVEY_YEAR" in source
    assert "survey_date_start" in source
    assert bluetopo.MIN_SURVEY_YEAR >= 1960


def test_a_grid_staged_under_another_cutoff_is_restaged():
    """"It exists" is not "it is current".

    Changing the cutoff changes what every grid should contain, and the state
    file is what decides whether a tile is pulled again. This pipeline has lost
    a day more than once to a resume that skipped work whose inputs had moved.
    """
    source = inspect.getsource(bluetopo.stage_region)
    assert "min_survey_year" in source
    assert "min_coverage" in source, "the coverage floor also changes every grid"
    assert "stale" in source


def test_the_hr_archive_declares_the_bounds_it_holds():
    """A declared box is not a label: the reader skips tiles outside it.

    bathy_hr's bounds were EMODnet's extent, which was true while EMODnet's
    grids were the only HR source and silently false the moment BlueTopo was
    added. The California preview held 370,652 tiles off Monterey and
    advertised 36W to 43E, so the viewer never asked for one and drew GEBCO.
    Reading tiles straight out of the archive bypasses the check, which is why
    every measurement of it looked right.
    """
    import inspect

    from basemap.pipeline import bathymetry

    source = inspect.getsource(bathymetry.hr_extent)
    assert "band.sources[: band.primary]" in source, "bounds must come from the sources"
    assert "render_bbox" in source, "a preview holds only its box"
    assert "hr_extent(" in inspect.getsource(bathymetry.run)


def test_an_hr_preview_declares_only_its_box(monkeypatch):
    """A box-limited render must not advertise the whole footprint."""
    from basemap.pipeline import bathymetry
    from basemap.processing.raster_tiles import Band, RenderSpec, Source

    spec = RenderSpec(
        (Band(12, 15, (Source("a", bounds=(-130.0, 30.0, -110.0, 45.0)),
                       Source("b", bounds=(-10.0, 35.0, 10.0, 60.0))), primary=2),),
        None, None,
    )
    monkeypatch.setenv("BASEMAP_RENDER_BBOX", "-125.2,32.0,-116.5,42.2")
    west, south, east, north = bathymetry.hr_extent(spec)["bounds"]
    assert (west, south, east, north) == (-125.2, 32.0, -116.5, 42.2)

    monkeypatch.delenv("BASEMAP_RENDER_BBOX")
    west, south, east, north = bathymetry.hr_extent(spec)["bounds"]
    assert west == -130.0 and east == 10.0, "without a box it is the union of the sources"


def test_a_grid_that_no_longer_qualifies_leaves_the_disk():
    """Returning before the write leaves the previous run's file in place.

    stage_one skips writing when a grid falls below the coverage floor, so a
    file staged under an older rule survives on disk and bluetopo_sources
    globs it straight back into the stack -- the floor would have changed
    nothing. Caught with 6,237 files on disk against 2,862 grids kept.
    """
    import inspect

    source = inspect.getsource(bluetopo.stage_region)
    assert "unlink(missing_ok=True)" in source
    assert source.index("unlink") < source.index('glob("*.tif")')


def test_several_regions_stage_in_one_run_each_with_its_own_state(monkeypatch):
    monkeypatch.setenv("BASEMAP_BLUETOPO_REGION", "conus alaska prusvi")
    assert bluetopo.wanted_regions() == ["conus", "alaska", "prusvi"]
    monkeypatch.delenv("BASEMAP_BLUETOPO_REGION")
    assert bluetopo.wanted_regions() == ["conus"]
    monkeypatch.setenv("BASEMAP_BLUETOPO_REGION", "conus hawaii")
    with pytest.raises(SystemExit, match="hawaii"):
        bluetopo.wanted_regions()
    assert 'f"staged_{region}.json"' in inspect.getsource(bluetopo.stage_region)


def test_a_tile_rejected_as_too_thin_is_not_pulled_again():
    source = inspect.getsource(bluetopo.stage_region)
    assert 'state[e["tile"]].get("kept")' in source
