"""Contours baked into the flat tile tree."""

from __future__ import annotations

import numpy as np
import pytest

from basemap.pipeline import xyz


class FakeArchive:
    """Stands in for a PMTiles archive holding one decoded tile's worth."""

    def __init__(self, geometry):
        self.geometry = geometry
        self.asked: list[tuple[int, int, int]] = []

    def covers(self, z, west, south, east, north):
        return True

    def get(self, z, x, y):
        self.asked.append((z, x, y))
        return b"stub"


def _contours(monkeypatch, geometry):
    """A Contours whose decode returns *geometry*, skipping PMTiles and MVT."""
    contours = xyz.Contours.__new__(xyz.Contours)
    contours._archive = FakeArchive(geometry)
    contours._key = None
    contours._geom = []
    monkeypatch.setattr(xyz.Contours, "_decode", lambda self, z, x, y: geometry)
    return contours


# --- geometry --------------------------------------------------------------

def test_a_line_across_the_source_tile_spans_the_tile(monkeypatch):
    """Unit coordinates 0..1 become 0..TILE, at CONTOUR_SUPERSAMPLE."""
    line = np.array([[0.0, 0.5], [1.0, 0.5]], dtype=np.float32)
    contours = _contours(monkeypatch, [(True, line)])
    ((major, points),) = contours.lines(5, 3, 4)
    assert major
    edge = xyz.TILE * xyz.CONTOUR_SUPERSAMPLE
    assert points[0][0] == pytest.approx(0.0)
    assert points[1][0] == pytest.approx(edge)
    assert points[0][1] == pytest.approx(edge / 2)


def test_a_child_tile_takes_its_quarter_of_the_parent(monkeypatch):
    """Above the archive's last level the geometry is re-projected, not scaled.

    The line stays one pixel wide at z12 because it is drawn again at z12,
    rather than a z10 image being stretched sixteen times.
    """
    line = np.array([[0.0, 0.5], [1.0, 0.5]], dtype=np.float32)
    contours = _contours(monkeypatch, [(False, line)])
    edge = xyz.TILE * xyz.CONTOUR_SUPERSAMPLE

    z = xyz.CONTOUR_SOURCE_MAX_ZOOM + 2
    parent_x, parent_y = 300, 200
    # The child two levels down that holds the middle-left of the parent.
    ((_, points),) = contours.lines(z, parent_x << 2, (parent_y << 2) + 2)
    assert points[0][0] == pytest.approx(0.0)
    assert points[1][0] == pytest.approx(edge * 4)       # runs across four children
    assert points[0][1] == pytest.approx(edge * 4 / 2 - 2 * edge)


def test_the_source_tile_is_decoded_once_per_family(monkeypatch):
    line = np.array([[0.0, 0.5], [1.0, 0.5]], dtype=np.float32)
    calls = []

    contours = xyz.Contours.__new__(xyz.Contours)
    contours._archive = FakeArchive(None)
    contours._key = None
    contours._geom = []

    def decode(self, z, x, y):
        calls.append((z, x, y))
        return [(False, line)]

    monkeypatch.setattr(xyz.Contours, "_decode", decode)
    for dy in range(4):                       # four children of one z10 tile
        contours.lines(12, 400, 800 + dy)
    assert len(calls) == 1, f"decoded {len(calls)} times for one parent"


def test_lines_outside_the_tile_are_dropped(monkeypatch):
    far = np.array([[0.90, 0.90], [0.99, 0.99]], dtype=np.float32)
    contours = _contours(monkeypatch, [(False, far)])
    z = xyz.CONTOUR_SOURCE_MAX_ZOOM + 2
    assert contours.lines(z, 0, 0) == []      # top-left child cannot see it
    assert contours.lines(z, 3, 3) != []      # bottom-right child can


def test_an_empty_source_tile_costs_nothing(monkeypatch):
    contours = _contours(monkeypatch, [])
    assert contours.lines(8, 5, 5) == []


# --- depth selection -------------------------------------------------------

def test_only_multiples_of_the_step_are_kept():
    depths = [-500, -1000, -1500, -2000, -400, -600, -1200, -2800]
    kept = [d for d in depths if d % xyz.CONTOUR_STEP == 0]
    assert kept == [-500, -1000, -1500, -2000]


def test_the_shallow_set_waits_for_a_zoom_where_it_is_legible():
    assert xyz.CONTOUR_SHALLOW_MIN_ZOOM > 0
    assert -50 in xyz.CONTOUR_SHALLOW
    assert all(d % xyz.CONTOUR_STEP for d in xyz.CONTOUR_SHALLOW), \
        "the shallow set must not duplicate the 500 m lines"


def test_the_major_weight_is_stronger_than_the_minor():
    assert xyz.CONTOUR_MAJOR_ALPHA > xyz.CONTOUR_MINOR_ALPHA
    assert xyz.CONTOUR_MAJOR_STEP % xyz.CONTOUR_STEP == 0


# --- drawing ---------------------------------------------------------------

def _flat(value=200):
    return np.full((xyz.TILE, xyz.TILE, 3), value, dtype=np.uint8)


def test_drawing_darkens_the_pixels_under_the_line():
    ss = xyz.CONTOUR_SUPERSAMPLE
    line = np.array([[0, xyz.TILE * ss / 2], [xyz.TILE * ss, xyz.TILE * ss / 2]],
                    dtype=np.float32)
    out = xyz._draw_contours(_flat(), [(True, line)], None)
    middle = out[xyz.TILE // 2]
    assert middle.min() < 200, "the line should show"
    assert out[5].min() == 200, "and only where it is"


def test_a_major_line_is_stronger_than_a_minor_one():
    ss = xyz.CONTOUR_SUPERSAMPLE
    line = np.array([[0, xyz.TILE * ss / 2], [xyz.TILE * ss, xyz.TILE * ss / 2]],
                    dtype=np.float32)
    minor = xyz._draw_contours(_flat(), [(False, line)], None)
    major = xyz._draw_contours(_flat(), [(True, line)], None)
    assert major[xyz.TILE // 2].min() < minor[xyz.TILE // 2].min()


def test_the_sea_mask_keeps_contours_off_the_land():
    ss = xyz.CONTOUR_SUPERSAMPLE
    line = np.array([[0, xyz.TILE * ss / 2], [xyz.TILE * ss, xyz.TILE * ss / 2]],
                    dtype=np.float32)
    sea = np.zeros((xyz.TILE, xyz.TILE), dtype=np.uint16)
    sea[:, : xyz.TILE // 2] = 1                       # sea on the left only
    out = xyz._draw_contours(_flat(), [(True, line)], sea)
    row = out[xyz.TILE // 2]
    assert row[: xyz.TILE // 2].min() < 200
    assert row[xyz.TILE // 2 + 2:].min() == 200


def test_nothing_drawn_leaves_the_tile_untouched():
    base = _flat()
    assert np.array_equal(xyz._draw_contours(base, [], None), base)


# --- compose ---------------------------------------------------------------

def test_an_ocean_tile_with_no_contour_is_passed_through_unencoded(monkeypatch):
    """The fast path is the whole reason this step finishes in a day."""
    sentinel = b"the archive's own bytes"

    class Parts:
        def raw(self, names, z, x, y):
            return sentinel if "bathy_global" in names else None

    monkeypatch.setattr(xyz, "_is_opaque", lambda image: True)
    monkeypatch.setattr(xyz.Image, "open", lambda buf: _FakeImage())

    class Empty:
        def lines(self, z, x, y):
            return []

    assert xyz.compose(Parts(), 4, 1, 1, Empty()) is sentinel


class _FakeImage:
    mode = "RGB"

    def convert(self, mode):
        return self


def test_the_worker_opens_the_contour_archive_when_it_exists(tmp_path, monkeypatch):
    opened = {}
    monkeypatch.setattr(xyz.Composite, "open", classmethod(lambda cls, d, n: "parts"))
    monkeypatch.setattr(xyz, "Contours", lambda path: opened.setdefault("path", path))
    monkeypatch.setattr(xyz, "Water", lambda path: opened.setdefault("work", path))
    monkeypatch.setattr(xyz, "compose", lambda *a, **k: None)
    xyz._WORKER.clear()

    (tmp_path / f"{xyz.CONTOUR_ARCHIVE}.pmtiles").write_bytes(b"")
    work = tmp_path / "elsewhere"
    xyz._render_batch((tmp_path, work, 0, [0], 0, 1))
    assert opened["work"] == work, "the work directory is passed, not guessed"
    assert opened["path"].name == f"{xyz.CONTOUR_ARCHIVE}.pmtiles"
    xyz._WORKER.clear()


# --- inland water ----------------------------------------------------------

def _work(tmp_path):
    """A work directory with the layers the compositor insists on."""
    (tmp_path / "lakes").mkdir(exist_ok=True)
    gsw = tmp_path / "water" / "gsw"
    gsw.mkdir(parents=True, exist_ok=True)
    (gsw / "gsw_occurrence.vrt").write_text("<VRTDataset/>")
    return tmp_path


def test_each_zoom_takes_a_lake_area_band(tmp_path):
    """The flat tree uses the same area thresholds as the vector map."""
    from basemap.config import LAKE_BANDS

    seen = set()
    for _, (lo, hi) in LAKE_BANDS:
        for z in range(lo, hi + 1):
            seen.add(z)
            assert xyz._lake_band_file(tmp_path, z) is not None
    assert seen == set(range(2, 13)), "z2-12 must all be covered"


def test_every_zoom_the_tree_renders_is_handled(tmp_path, monkeypatch):
    """The tree renders z0-12; the lake bands start at z2.

    This is the bug that killed an eleven-hour build on its first column:
    the guard against a silently lakeless tree fired at z0, where having no
    band is the correct answer rather than a missing one.
    """
    water = xyz.Water(_work(tmp_path))
    for z in range(0, 2):
        assert xyz._lake_band_file(tmp_path, z) is None
        assert water.at(z, 0, 0) == ([], []), f"z{z} must render without lakes"


def test_the_band_narrows_as_the_zoom_grows(tmp_path):
    """Tiling every pond from z2 would bury the lakes legible there."""
    low = xyz._lake_band_file(tmp_path, 2).name
    high = xyz._lake_band_file(tmp_path, 12).name
    assert low != high
    assert high.endswith("all.fgb")


def test_missing_lakes_is_an_error_not_an_empty_map(tmp_path):
    """The first run of the tree shipped with no lakes and said nothing."""
    with pytest.raises(SystemExit, match="lakes"):
        xyz.Water(tmp_path)


def test_a_lake_band_absent_from_disk_is_an_error(tmp_path, monkeypatch):
    water = xyz.Water(_work(tmp_path))
    with pytest.raises(SystemExit, match="lakes step"):
        water.at(7, 0, 0)


def test_a_missing_occurrence_mosaic_is_an_error(tmp_path):
    """Coastal water is a layer too; its absence must not be silent either."""
    (tmp_path / "lakes").mkdir()
    with pytest.raises(SystemExit, match="surface-water"):
        xyz.Water(tmp_path)


def test_an_inland_lake_takes_the_lake_ramp():
    shallow = xyz._water_colour(2.0, 300.0)
    deep = xyz._water_colour(200.0, 300.0)
    assert sum(deep) < sum(shallow), "deeper water should be darker"
    assert shallow[2] > shallow[0], "lake water is blue"


def test_a_coastal_lake_takes_the_sea_ramp():
    """A lake at sea level sits against the sea and should read like it."""
    from basemap.config import COASTAL_LAKE_ELEV

    inland = xyz._water_colour(20.0, COASTAL_LAKE_ELEV + 50)
    coastal = xyz._water_colour(20.0, 0.0)
    assert inland != coastal


def test_rings_and_parts_walk_multi_geometries():
    from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon

    square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    holed = Polygon([(0, 0), (0, 4), (4, 4), (4, 0)],
                    [[(1, 1), (1, 2), (2, 2), (2, 1)]])
    assert len(xyz._rings(square)) == 1
    assert len(xyz._rings(MultiPolygon([square, holed]))) == 2
    assert len(xyz._rings(holed)[0][1]) == 1, "the hole must survive"
    line = LineString([(0, 0), (1, 1)])
    assert len(xyz._parts(line)) == 1
    assert len(xyz._parts(MultiLineString([line, line]))) == 2


def _tile_polygon(z, x, y, shrink=0.25):
    """A polygon covering the middle of a tile, in Web Mercator."""
    from shapely.geometry import box

    from basemap.processing.raster_tiles import MERCATOR

    w, s, e, n = MERCATOR.tile_bounds(z, x, y)
    dx, dy = (e - w) * shrink, (n - s) * shrink
    return box(w + dx, s + dy, e - dx, n - dy)


def test_a_lake_is_painted_inside_and_not_outside():
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, dtype=np.uint8)
    lake = (_tile_polygon(6, 33, 22), 30.0, 400.0)
    out = xyz._draw_water(base, [lake], [], 6, 33, 22)
    middle = out[xyz.TILE // 2, xyz.TILE // 2]
    assert not np.array_equal(middle, base[xyz.TILE // 2, xyz.TILE // 2])
    assert middle[2] > middle[0], "a lake should be blue"
    assert np.array_equal(out[2, 2], base[2, 2]), "the corner is still land"


def test_a_deeper_lake_is_darker_than_a_shallow_one():
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, dtype=np.uint8)
    poly = _tile_polygon(6, 33, 22)
    shallow = xyz._draw_water(base, [(poly, 2.0, 400.0)], [], 6, 33, 22)
    deep = xyz._draw_water(base, [(poly, 500.0, 400.0)], [], 6, 33, 22)
    mid = (xyz.TILE // 2, xyz.TILE // 2)
    assert int(deep[mid].sum()) < int(shallow[mid].sum())


def test_nothing_to_draw_leaves_the_tile_alone():
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, dtype=np.uint8)
    assert xyz._draw_water(base, [], [], 6, 33, 22) is base


def test_a_river_marks_the_tile_it_crosses():
    from shapely.geometry import LineString

    from basemap.processing.raster_tiles import MERCATOR

    w, s, e, n = MERCATOR.tile_bounds(6, 33, 22)
    river = LineString([(w, (s + n) / 2), (e, (s + n) / 2)])
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, dtype=np.uint8)
    out = xyz._draw_water(base, [], [river], 6, 33, 22)
    assert not np.array_equal(out[xyz.TILE // 2], base[xyz.TILE // 2])
    assert np.array_equal(out[5], base[5])


def test_lakes_are_drawn_over_rivers():
    """A river meeting a lake should run into it, not across it."""
    from shapely.geometry import LineString

    from basemap.processing.raster_tiles import MERCATOR

    w, s, e, n = MERCATOR.tile_bounds(6, 33, 22)
    river = LineString([(w, (s + n) / 2), (e, (s + n) / 2)])
    lake = (_tile_polygon(6, 33, 22), 40.0, 400.0)
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, dtype=np.uint8)
    both = xyz._draw_water(base, [lake], [river], 6, 33, 22)
    just_lake = xyz._draw_water(base, [lake], [], 6, 33, 22)
    mid = (xyz.TILE // 2, xyz.TILE // 2)
    assert np.array_equal(both[mid], just_lake[mid])


def test_compose_takes_water():
    import inspect

    assert "water" in inspect.signature(xyz.compose).parameters


def test_the_worker_is_given_the_work_directory():
    """Not derived from the tile directory: that is how it pointed nowhere."""
    import inspect

    source = inspect.getsource(xyz._render_batch)
    assert "work_dir" in source
    assert "tile_dir.parent" not in source


# --- versioned publishing --------------------------------------------------

def test_the_tree_defaults_to_the_live_prefix(monkeypatch):
    monkeypatch.delenv("BASEMAP_XYZ_PREFIX", raising=False)
    assert xyz.tree_prefix() == xyz.DEFAULT_PREFIX


def test_a_version_publishes_under_its_own_name(monkeypatch):
    monkeypatch.delenv("BASEMAP_XYZ_PREFIX", raising=False)
    monkeypatch.setenv("BASEMAP_XYZ_VERSION", "v2")
    assert xyz.tree_prefix() == "basemap/v2/xyz"


def test_an_empty_version_still_means_live(monkeypatch):
    """The job always sets the variable; unset and empty must behave alike."""
    monkeypatch.delenv("BASEMAP_XYZ_PREFIX", raising=False)
    monkeypatch.setenv("BASEMAP_XYZ_VERSION", "")
    assert xyz.tree_prefix() == xyz.DEFAULT_PREFIX


def test_an_explicit_prefix_still_wins(monkeypatch):
    monkeypatch.setenv("BASEMAP_XYZ_VERSION", "v2")
    monkeypatch.setenv("BASEMAP_XYZ_PREFIX", "/basemap/somewhere/xyz/")
    assert xyz.tree_prefix() == "basemap/somewhere/xyz"


def test_send_marks_are_per_prefix(tmp_path):
    """Publishing one stage to a new prefix must send every column again."""
    stage = tmp_path / "xyz.stage.mbtiles"
    live = xyz._marks_path(stage, xyz.DEFAULT_PREFIX)
    first = xyz._marks_path(stage, "basemap/v2/xyz")
    second = xyz._marks_path(stage, "basemap/v3/xyz")
    assert live != first != second and live != second
    assert xyz._marks_path(stage, "basemap/v2/xyz") == first, "and be stable"


def test_a_missing_contour_archive_is_an_error(tmp_path):
    """The first published tree had no contours and nothing said so.

    An absent archive used to evaluate to None and draw nothing, which in the
    output is indistinguishable from a tile with no contour crossing it.
    """
    with pytest.raises(SystemExit, match="depth_contours"):
        xyz._require_contours(tmp_path)


def test_the_contour_archive_is_checked_before_rendering():
    """Alongside the lakes, so a missing layer costs a second, not a day."""
    import inspect

    body = inspect.getsource(xyz.run)
    assert "_require_contours" in body
    assert body.index("Water(work_dir)") < body.index("Columns")


# --- coastal water ---------------------------------------------------------

def test_coastal_water_is_painted_where_it_is_observed():
    base = np.full((xyz.TILE, xyz.TILE, 3), 210, dtype=np.uint8)
    cover = np.zeros((xyz.TILE, xyz.TILE), dtype=np.float32)
    cover[40:80, 40:80] = 1.0
    out = xyz._draw_coastal(base, cover, np.ones_like(cover))
    assert tuple(out[60, 60]) == xyz.COASTAL_WATER_RGB
    assert tuple(out[5, 5]) == (210, 210, 210)


def test_coastal_water_never_touches_the_sea():
    """Occurrence marks the coastal ocean too; painting it erases bathymetry."""
    base = np.full((xyz.TILE, xyz.TILE, 3), 210, dtype=np.uint8)
    cover = np.ones((xyz.TILE, xyz.TILE), dtype=np.float32)
    on_land = np.zeros_like(cover)          # all sea
    assert np.array_equal(xyz._draw_coastal(base, cover, on_land), base)


def test_the_coastal_edge_is_soft():
    """A hard cut turned the lagoon's channels into blobs."""
    base = np.full((xyz.TILE, xyz.TILE, 3), 210, dtype=np.uint8)
    half = np.full((xyz.TILE, xyz.TILE), 0.5, dtype=np.float32)
    out = xyz._draw_coastal(base, half, np.ones_like(half))
    mid = out[60, 60]
    assert tuple(mid) != xyz.COASTAL_WATER_RGB and tuple(mid) != (210, 210, 210)


def test_the_coastal_band_spans_the_occurrence_range():
    assert 0 < xyz.COASTAL_WATER_LOW < xyz.COASTAL_WATER_HIGH <= 100


def test_no_coverage_leaves_the_tile_alone():
    base = np.full((xyz.TILE, xyz.TILE, 3), 210, dtype=np.uint8)
    assert xyz._draw_coastal(base, None, None) is base


def test_a_shallow_coastal_lake_matches_the_water_around_it():
    """The lagoon's fish-farm basins came out as white holes in a blue lagoon.

    They are HydroLAKES polygons a metre deep at -4 m elevation, so they take
    the sea ramp -- whose shallow end went near-white when it was matched to
    EMODnet. Flooring them at the coastal colour is what the rule meant all
    along: read like the water you sit against.
    """
    assert xyz._water_colour(0.9, -4.0) == xyz.COASTAL_WATER_RGB


def test_an_inland_lake_still_uses_the_lake_ramp():
    shallow = xyz._water_colour(2.0, 300.0)
    deep = xyz._water_colour(500.0, 300.0)
    assert shallow != xyz.COASTAL_WATER_RGB
    assert sum(deep) < sum(shallow)


def test_the_floor_only_lightens_never_darkens():
    """A coastal body deeper than the floor keeps its own tone."""
    import numpy as np

    floor = float(np.mean(xyz.COASTAL_WATER_RGB))
    deep = xyz._water_colour(2000.0, 0.0)
    assert float(np.mean(deep)) <= floor


def test_the_strip_read_is_only_used_where_it_agrees(tmp_path):
    """Below z11 a strip spans too much latitude to match the per-tile read."""
    assert xyz.COVER_STRIP_MIN_ZOOM >= 11


def test_all_sea_tiles_skip_the_occurrence_read():
    """It is the most expensive thing in a tile and most of the world is sea."""
    import inspect

    body = inspect.getsource(xyz._paint_water)
    assert "on_land.any()" in body
