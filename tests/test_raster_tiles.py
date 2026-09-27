"""Tests for the continuous relief renderer and the sources it reads."""

from __future__ import annotations

import io
import math
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_bounds
from shapely import box

from basemap.cli import FULL_ORDER, STEPS
from basemap.pipeline.bathymetry import grid_sign
from basemap.processing import copernicus
from basemap.processing import raster_tiles as rt
from basemap.processing.raster_tiles import (
    Band,
    Ramp,
    RenderSpec,
    Shading,
    Source,
    hillshade,
    land_fraction,
    read_elevation,
    render_into,
    render_tile,
    shade,
    stage_path,
    tile_bounds,
    tile_lonlat_bounds,
)

RAMP = Ramp.parse("""\
# comment
-100 0 0 100
0    100 100 200
nv   0 0 0 0
""")


def _geotiff(path: Path, data: np.ndarray, bounds: tuple, nodata=None) -> Path:
    h, w = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_bounds(*bounds, w, h), nodata=nodata,
    ) as ds:
        ds.write(data.astype("float32"), 1)
    return path


@pytest.fixture(autouse=True)
def _close_cached_datasets():
    """The renderer keeps datasets open per process; close them with the test.

    Otherwise they outlive the temporary files they point at, and closing them
    at interpreter exit spews errors.
    """
    yield
    for ds in rt._DATASETS.values():
        ds.close()
    rt._DATASETS.clear()
    rt._OVERVIEWS.clear()


# A z10 tile off Lisbon, and a box comfortably around it.
Z, X, Y = 10, 485, 395
AROUND = (-10.0, 37.5, -8.5, 39.5)


class TestTileMaths:
    def test_world_tile_spans_the_mercator_square(self):
        w, s, e, n = tile_bounds(0, 0, 0)
        assert (w, e) == pytest.approx((-rt.ORIGIN, rt.ORIGIN))
        assert (s, n) == pytest.approx((-rt.ORIGIN, rt.ORIGIN))

    def test_point_lands_in_its_own_tile(self):
        lon, lat = -9.15, 38.7
        x, y = rt.lonlat_to_tile(lon, lat, 12)
        w, s, e, n = tile_lonlat_bounds(12, x, y)
        assert w <= lon < e and s <= lat < n

    def test_tiles_in_bbox_cover_the_box(self):
        tiles = set(rt.tiles_in_bbox((-10, 38, -9, 39), 8))
        for lon, lat in [(-10, 38), (-9.01, 38.99), (-9.5, 38.5)]:
            assert rt.lonlat_to_tile(lon, lat, 8) in tiles

    def test_mercator_matches_the_projection_formula(self):
        coords = np.array([[-9.0, 38.0], [180.0, 0.0]])
        out = rt.MERCATOR.to_projected(coords)
        assert out[0, 1] == pytest.approx(6378137.0 * math.asinh(math.tan(math.radians(38.0))))
        assert out[1, 0] == pytest.approx(rt.ORIGIN)


class TestRamp:
    def test_parse_skips_comments_and_nodata(self):
        assert RAMP.elevation == (-100.0, 0.0)

    def test_colour_at_a_stop(self):
        assert RAMP.colour(np.array([0.0]))[0] == pytest.approx([100, 100, 200])

    def test_colour_between_stops_is_interpolated(self):
        assert RAMP.colour(np.array([-50.0]))[0] == pytest.approx([50, 50, 150])

    def test_colour_clamps_beyond_the_ends(self):
        assert RAMP.colour(np.array([-5000.0]))[0] == pytest.approx([0, 0, 100])

    def test_rejects_a_single_stop(self):
        with pytest.raises(ValueError):
            Ramp.parse("0 1 2 3")


class TestShading:
    def test_flat_ground_is_lit_exactly_as_flat(self):
        light = hillshade(np.full((10, 10), -50.0), 30.0, Shading())
        assert light.shape == (8, 8)
        assert light == pytest.approx(np.ones((8, 8)))

    def test_flat_ground_keeps_its_ramp_colour(self):
        rgb = np.full((4, 4, 3), 120.0)
        assert shade(rgb, np.ones((4, 4)), Shading()) == pytest.approx(rgb)

    def test_steep_shadow_stops_at_the_floor(self):
        out = shade(np.full((1, 1, 3), 200.0), np.zeros((1, 1)), Shading(strength=1, floor=0.6))
        assert out[0, 0, 0] == pytest.approx(120.0)

    def test_slope_facing_the_light_is_brighter(self):
        # Rising to the south-east means the slope faces the north-west sun.
        yy, xx = np.mgrid[0:10, 0:10]
        light = hillshade((xx + yy) * 5.0, 30.0, Shading(azimuth=315))
        assert light.mean() > 1.0

    def test_exaggeration_grows_with_pixel_size_and_is_capped(self):
        s = Shading(reference_m=25, power=0.5, max_exaggeration=10)
        assert s.exaggeration(10) == 1.0
        assert s.exaggeration(100) == pytest.approx(2.0)
        assert s.exaggeration(1e7) == 10


class TestLandFraction:
    def test_no_land_is_zero(self):
        assert land_fraction([], Z, X, Y).max() == 0

    def test_half_land_is_half_with_a_graded_edge(self):
        w, s, e, n = tile_bounds(Z, X, Y)
        # West half, with the edge through the middle of a pixel column.
        edge = w + (e - w) * (0.5 + 0.5 / rt.TILE)
        frac = land_fraction([box(w - 1, s - 1, edge, n + 1)], Z, X, Y)
        assert frac.mean() == pytest.approx(0.5, abs=0.01)
        assert ((frac > 0) & (frac < 1)).any()


class TestSeaAlpha:
    """The bathymetry's alpha stops short of the shore, so an overzoomed tile's
    blend never reaches the land (see sea_alpha)."""

    def _half(self, edge_frac):
        w, s, e, n = tile_bounds(Z, X, Y)
        edge = w + (e - w) * edge_frac
        return [box(w - 1, s - 1, edge, n + 1)], edge

    def test_alpha_is_opaque_or_transparent(self):
        land, _ = self._half(0.5)
        alpha = rt.sea_alpha(land, Z, X, Y)
        assert set(np.unique(alpha)) <= {0.0, 255.0}

    def test_opaque_pixels_keep_a_pixel_clear_of_land(self):
        land, _ = self._half(0.5 + 0.3 / rt.TILE)  # edge inside a pixel column
        alpha = rt.sea_alpha(land, Z, X, Y)
        frac = land_fraction(land, Z, X, Y)
        opaque_cols = np.where(alpha[0] == 255)[0]
        last_land_col = np.where(frac[0] > 0)[0].max()
        assert opaque_cols.min() >= last_land_col + 2

    def test_land_just_past_the_tile_edge_still_clears_it(self):
        w, s, e, n = tile_bounds(Z, X, Y)
        px = (e - w) / rt.TILE
        land = [box(e + 0.2 * px, s - 1, e + 5 * px, n + 1)]  # east of the tile
        alpha = rt.sea_alpha(land, Z, X, Y)
        assert (alpha[:, -1] == 0).all() and (alpha[:, 0] == 255).all()

    def test_open_sea_is_fully_opaque(self):
        assert (rt.sea_alpha([], Z, X, Y) == 255).all()


class TestReadElevation:
    def test_gaps_fall_through_to_the_next_source(self, tmp_path):
        hr = np.full((200, 150), -5.0)
        hr[:, 75:] = np.nan
        fine = _geotiff(tmp_path / "hr.tif", hr, AROUND, nodata=np.nan)
        coarse = _geotiff(tmp_path / "dtm.tif", np.full((50, 40), -80.0), AROUND)
        dem, hit = read_elevation([Source(str(fine)), Source(str(coarse))], Z, X, Y)
        assert hit
        assert not np.isnan(dem).any()
        assert set(np.unique(dem.round())) <= {-80.0, -5.0}

    def test_sign_flips_positive_down_grids(self, tmp_path):
        grid = _geotiff(tmp_path / "g.tif", np.full((50, 40), 30.0), AROUND)
        dem, _ = read_elevation([Source(str(grid), sign=-1)], Z, X, Y)
        assert np.nanmax(dem) == pytest.approx(-30.0)

    def test_primary_miss_is_reported(self, tmp_path):
        far = _geotiff(tmp_path / "far.tif", np.full((10, 10), -5.0), (20, 20, 21, 21))
        dtm = _geotiff(tmp_path / "dtm.tif", np.full((50, 40), -80.0), AROUND)
        sources = [Source(str(far), bounds=(20, 20, 21, 21)), Source(str(dtm))]
        _, hit = read_elevation(sources, Z, X, Y, primary=1)
        assert not hit

    def test_values_outside_the_valid_range_are_gaps(self, tmp_path):
        grid = _geotiff(tmp_path / "g.tif", np.full((50, 40), -32768.0), AROUND)
        dem, _ = read_elevation([Source(str(grid))], Z, X, Y)
        assert np.isnan(dem).all()

    def test_pyramid_picks_the_coarsest_that_is_still_fine_enough(self):
        src = Source("full", pyramid=((0.001, "x2"), (0.002, "x4"), (0.004, "x8")))
        assert src.at(0.003) == "x4"
        assert src.at(0.0005) == "full"


def _spec(tmp_path, *, mask=None, bands=None) -> RenderSpec:
    dtm = _geotiff(tmp_path / "dtm.tif", np.full((50, 40), -40.0), AROUND)
    return RenderSpec(
        bands or (Band(0, 16, (Source(str(dtm)),)),), RAMP, mask=mask, clamp=(-12000, 0),
    )


class TestRenderTile:
    def test_sea_tile_is_opaque_webp(self, tmp_path):
        data = render_tile(_spec(tmp_path, mask="sea"), Z, X, Y, land=[])
        img = Image.open(io.BytesIO(data))
        assert img.format == "WEBP"
        assert np.asarray(img.convert("RGBA"))[..., 3].min() == 255

    def test_all_land_tile_is_skipped_for_the_sea(self, tmp_path):
        w, s, e, n = tile_bounds(Z, X, Y)
        land = [box(w - 1, s - 1, e + 1, n + 1)]
        assert render_tile(_spec(tmp_path, mask="sea"), Z, X, Y, land=land) is None

    def test_tile_without_land_is_skipped_for_the_land(self, tmp_path):
        assert render_tile(_spec(tmp_path, mask="land"), Z, X, Y, land=[]) is None

    def test_tile_with_no_data_is_skipped(self, tmp_path):
        spec = _spec(tmp_path)
        assert render_tile(spec, 10, 0, 0) is None  # the Arctic, far outside the grid


class TestRenderInto:
    def test_tiles_are_staged_and_marked_done(self, tmp_path):
        stage = stage_path(tmp_path / "out.pmtiles")
        render_into(stage, _spec(tmp_path), [(Z, X, Y)], label="t", workers=1)
        db = sqlite3.connect(stage)
        assert db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 1

    def test_a_failed_metatile_is_not_marked_done(self, tmp_path):
        stage = stage_path(tmp_path / "out.pmtiles")
        # No band covers z10, so the worker raises.
        spec = _spec(tmp_path, bands=(Band(0, 5, ()),))
        with pytest.raises(ValueError):
            render_into(stage, spec, [(Z, X, Y)], label="t", workers=1)
        db = sqlite3.connect(stage)
        assert db.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 0


class TestFinalize:
    def test_staged_tiles_become_a_readable_pmtiles_archive(self, tmp_path):
        from pmtiles.reader import MmapSource, Reader

        dst = tmp_path / "out.pmtiles"
        render_into(stage_path(dst), _spec(tmp_path), [(Z, X, Y)], label="t", workers=1)
        rt.finalize(stage_path(dst), dst, name="t")
        assert not stage_path(dst).exists()
        with open(dst, "rb") as f:
            reader = Reader(MmapSource(f))
            assert reader.header()["tile_type"].name == "WEBP"
            assert Image.open(io.BytesIO(reader.get(Z, X, Y))).format == "WEBP"


class TestGridSign:
    def test_positive_down_grid_is_flipped(self, tmp_path):
        ref = _geotiff(tmp_path / "dtm.tif", np.full((50, 40), -30.0), AROUND)
        grid = _geotiff(tmp_path / "hr.tif", np.full((20, 20), 30.0), (-9.5, 38.5, -9.0, 39.0))
        assert grid_sign(str(grid), str(ref)) == -1

    def test_coastal_grid_is_judged_on_its_own_pixels(self, tmp_path):
        # The reference is land (positive) across most of the grid's box, which
        # is what fooled a median over the box; only the grid's pixels count.
        dtm = np.full((50, 40), 200.0)
        dtm[:, :5] = -30.0
        ref = _geotiff(tmp_path / "dtm.tif", dtm, AROUND)
        hr = np.full((20, 20), np.nan)
        hr[:, :2] = 30.0  # positive-down depths, only on the sea side
        grid = _geotiff(tmp_path / "hr.tif", hr, (-10.0, 38.5, -8.5, 39.0), nodata=np.nan)
        assert grid_sign(str(grid), str(ref)) == -1


class TestCopernicus:
    def test_cell_parses_the_south_west_corner(self):
        assert copernicus.cell("Copernicus_DSM_COG_10_N38_00_W010_00_DEM") == (-10, 38)
        assert copernicus.cell("Copernicus_DSM_COG_30_S54_00_E073_00_DEM") == (73, -54)

    def test_url_does_not_repeat_the_dem_suffix(self):
        tile = "Copernicus_DSM_COG_10_N38_00_W010_00_DEM"
        assert copernicus.GLO30.url(tile).endswith(f"/{tile}/{tile}.tif")

    def test_inside_uses_the_corner(self):
        assert copernicus.inside("Copernicus_DSM_COG_10_N38_00_W010_00_DEM", (-36, 15, 43, 84))
        assert not copernicus.inside("Copernicus_DSM_COG_10_N38_00_W040_00_DEM", (-36, 15, 43, 84))


class TestRegistered:
    def test_bathymetry_runs_after_its_sources(self):
        assert "bathymetry" in STEPS
        order = FULL_ORDER.index
        assert order("bathymetry") > max(order("depth-polygons"), order("land-tiles"))
        assert order("bathymetry") < order("upload")


class TestMemoryBounds:
    """A metatile's extent is how much coastline one worker holds at once.

    Unbounded, a z0-6 metatile loaded OSM land for a continent -- 8.8 GiB per
    worker, which got the pod evicted with 8 of them running.
    """

    @pytest.mark.parametrize("z", range(0, 17))
    def test_no_metatile_spans_more_than_a_z7_tile(self, z):
        side = rt.metatile_side(z)
        span = 360.0 / (1 << z) * side
        assert span <= 360.0 / (1 << 7) or side == 1

    def test_tiles_group_by_the_side_for_their_zoom(self):
        groups = rt.metatiles([(9, 0, 0), (9, 3, 3), (9, 4, 0)])
        assert set(groups) == {(9, 0, 0), (9, 1, 0)}

    def test_small_land_source_is_loaded_once_and_indexed(self, tmp_path):
        import json

        path = tmp_path / "land.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[-9, 38], [-8, 38], [-8, 39], [-9, 39], [-9, 38]]]}},
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]]}},
        ]}))
        try:
            near = rt.land_in(str(path), (-10.0, 37.0, -7.0, 40.0))
            assert len(near) == 1
            assert str(path) in rt._LAND  # cached for the next metatile
            assert rt.land_in(str(path), (0.0, 0.0, 1.0, 1.0)) == []
        finally:
            rt._LAND.pop(str(path), None)

    def test_open_datasets_are_capped_and_the_oldest_closed(self, tmp_path):
        grids = [
            _geotiff(tmp_path / f"g{i}.tif", np.full((4, 4), -10.0), AROUND)
            for i in range(rt.MAX_OPEN_DATASETS + 3)
        ]
        opened = [rt._dataset(str(g), 0.001) for g in grids]
        assert len(rt._DATASETS) == rt.MAX_OPEN_DATASETS
        assert opened[0].closed and not opened[-1].closed


class TestRamps:
    """The shipped ramps must be in the convention the renderer feeds them.

    The renderer works in elevation: depth below the sea, or below a lake's own
    surface, is negative. A ramp written in positive metres clamps every depth
    to its first stop, which is how the lake beds first rendered uniformly pale.
    """

    @pytest.mark.parametrize("name", ["bathymetry-emodnet.txt", "lakes.txt", "land-emodnet.txt"])
    def test_ramp_parses_and_spans_its_side_of_zero(self, name):
        from basemap.config import RAMP_DIR

        ramp = Ramp.parse((RAMP_DIR / name).read_text())
        assert len(ramp.elevation) >= 2
        below = name != "land-emodnet.txt"
        assert (min(ramp.elevation) < 0) is below
        deep, shallow = (-200.0, -5.0) if below else (2000.0, 50.0)
        assert not np.allclose(ramp.colour(np.array([deep])), ramp.colour(np.array([shallow])))


class TestInsideAlpha:
    """The mirror of sea_alpha, for lake beds."""

    def test_opaque_only_well_inside(self):
        w, s, e, n = tile_bounds(Z, X, Y)
        px = (e - w) / rt.TILE
        # Covers the tile except a 3-pixel margin on the west, and runs well
        # past the other edges -- inside_alpha looks a pixel beyond the tile.
        alpha = rt.inside_alpha([box(w + 3 * px, s - 9 * px, e + 9 * px, n + 9 * px)], Z, X, Y)
        assert set(np.unique(alpha)) <= {0.0, 255.0}
        assert (alpha[:, :3] == 0).all() and (alpha[:, 10:] == 255).all()

    def test_polygon_running_past_the_tile_leaves_no_edge(self):
        w, s, e, n = tile_bounds(Z, X, Y)
        px = (e - w) / rt.TILE
        alpha = rt.inside_alpha([box(w - 9 * px, s - 9 * px, e + 9 * px, n + 9 * px)], Z, X, Y)
        assert (alpha == 255).all()   # no trimmed border inside a larger lake

    def test_nothing_inside_is_transparent(self):
        assert not rt.inside_alpha([], Z, X, Y).any()


class TestIceRamp:
    """Ice is coloured by a second ramp chosen by a mask, not by elevation.

    A hypsometric ramp reads height as ground cover, so an ice sheet standing
    2-3 km high lands in its bare-rock browns: Greenland rendered as desert.
    Widening the land ramp towards white at that height would bleach every
    plateau with it, so the ice is masked instead.
    """

    ICE = Ramp.parse("0 250 250 255\n9000 250 250 255\n")

    def _spec_with_ice(self, tmp_path, ice_path):
        dem = _geotiff(tmp_path / "land.tif", np.full((50, 40), 2000.0), AROUND)
        return RenderSpec(
            (Band(0, 16, (Source(str(dem)),)),), RAMP,
            ice_ramp=self.ICE, ice_path=str(ice_path), clamp=(0, 9000),
        )

    def test_ice_polygon_recolours_only_what_it_covers(self, tmp_path):
        w, s, e, n = tile_bounds(Z, X, Y)
        spec = self._spec_with_ice(tmp_path, tmp_path / "ice.geojson")
        # Ice over the western half of the tile only.
        ice = [box(w, s, (w + e) / 2, n)]
        img = np.asarray(
            Image.open(io.BytesIO(render_tile(spec, Z, X, Y, ice=ice))).convert("RGB")
        ).astype(int)
        west, east = img[128, 40], img[128, 215]
        assert west[2] > west[0] or west.min() > 200, f"ice half not on the ice ramp: {west}"
        assert abs(int(east.max()) - int(east.min())) > 5 or east.max() < 200, (
            f"rock half took the ice ramp too: {east}"
        )
        assert not np.array_equal(west, east)

    def test_without_ice_polygons_nothing_changes(self, tmp_path):
        spec = self._spec_with_ice(tmp_path, tmp_path / "ice.geojson")
        plain = RenderSpec(spec.bands, RAMP, clamp=(0, 9000))
        with_mask = render_tile(spec, Z, X, Y, ice=[])
        assert with_mask == render_tile(plain, Z, X, Y)

    def test_ice_edge_is_graded_not_stepped(self, tmp_path):
        """The blend follows the mask's coverage fraction, so the margin is soft.

        A hard switch would draw the generalised Natural Earth ice edge as a
        staircase, which is the very artefact the continuous renderer exists
        to avoid.
        """
        w, s, e, n = tile_bounds(Z, X, Y)
        spec = self._spec_with_ice(tmp_path, tmp_path / "ice.geojson")
        edge = w + (e - w) * 0.5
        img = np.asarray(
            Image.open(
                io.BytesIO(render_tile(spec, Z, X, Y, ice=[box(w, s, edge, n)]))
            ).convert("RGB")
        ).astype(int)
        row = img[128, :, 0]
        # Between solid ice and solid rock there is at least one intermediate.
        band = row[120:136]
        assert len(set(band.tolist())) > 2, f"ice edge is a hard step: {band.tolist()}"


class TestGeographicProjection:
    """The geographic grid exists because Web Mercator has no poles.

    Mercator stops at 85.05 degrees, which drops the South Pole and 6.7% of
    Antarctica from every tileset built on it, along with as much of the
    Arctic Ocean. A geographic pyramid reaches 90 and is the scheme CesiumJS
    tiles in natively.
    """

    def test_world_is_two_tiles_wide_and_one_tall_at_z0(self):
        assert sorted(rt.GEOGRAPHIC.world_tiles(0)) == [(0, 0), (1, 0)]
        assert sorted(rt.MERCATOR.world_tiles(0)) == [(0, 0)]

    def test_z0_tiles_cover_the_whole_sphere(self):
        west = rt.GEOGRAPHIC.tile_lonlat_bounds(0, 0, 0)
        east = rt.GEOGRAPHIC.tile_lonlat_bounds(0, 1, 0)
        assert west == (-180.0, -90.0, 0.0, 90.0)
        assert east == (0.0, -90.0, 180.0, 90.0)

    def test_the_south_pole_is_inside_a_tile(self):
        """The test Mercator cannot pass, at any zoom."""
        for z in (0, 4, 8):
            x, y = rt.GEOGRAPHIC.lonlat_to_tile(0.0, -90.0, z)
            _, s, _, _ = rt.GEOGRAPHIC.tile_lonlat_bounds(z, x, y)
            assert s <= -89.999, f"z{z} bottom tile stops at {s}"
        _, s, _, _ = rt.MERCATOR.tile_lonlat_bounds(8, 128, 255)
        assert s > -86.0, "Mercator suddenly reaches the pole; this test is wrong"

    def test_round_trip_through_tile_addresses(self):
        for lon, lat in ((-179.0, 89.5), (0.0, 0.0), (179.0, -89.5), (30.0, -75.0)):
            for z in (2, 6):
                x, y = rt.GEOGRAPHIC.lonlat_to_tile(lon, lat, z)
                w, s, e, n = rt.GEOGRAPHIC.tile_lonlat_bounds(z, x, y)
                assert w <= lon <= e and s <= lat <= n, f"{lon},{lat} not in z{z} {x},{y}"

    def test_pixels_are_square_in_degrees_but_not_on_the_ground(self):
        """Which is why the hillshade has to be given both spacings."""
        dy, dx = rt.GEOGRAPHIC.pixel_ground_m(4, 0, 0)          # 67.5-90 N
        assert dy > dx * 5, f"polar pixel should be far taller than wide: {dy} {dx}"
        # The equator is always a tile edge, never a tile's middle, so the
        # nearest thing to an isotropic pixel is the row just below it.
        equator_dy, equator_dx = rt.GEOGRAPHIC.pixel_ground_m(8, 0, 128)
        assert equator_dx == pytest.approx(equator_dy, rel=0.01)
        mdy, mdx = rt.MERCATOR.pixel_ground_m(4, 8, 8)
        assert mdy == mdx

    def test_anisotropic_pixels_do_not_shade_a_flat_surface(self):
        """A squeezed pixel read as a square one would band the polar cap.

        With one spacing for both axes, the east-west gradient of a constant
        surface is still zero -- but a real slope is scaled wrongly, so the
        check is that a tall thin pixel gives the same light as a square one
        for ground that is genuinely flat, and a different one where it is not.
        """
        flat = np.full((10, 10), 1500.0)
        square = hillshade(flat, 100.0, Shading())
        thin = hillshade(flat, (100.0, 4.0), Shading())
        assert np.allclose(square, 1.0) and np.allclose(thin, 1.0)
        ramp_dem = np.tile(np.linspace(0, 900, 10), (10, 1))
        assert not np.allclose(
            hillshade(ramp_dem, 100.0, Shading()), hillshade(ramp_dem, (100.0, 4.0), Shading())
        )

    def test_polygons_are_not_reprojected_for_a_geographic_render(self, tmp_path):
        """Degrees are already the grid's units, so the mask needs no transform."""
        coords = np.array([[10.0, 40.0], [11.0, 41.0]])
        assert np.array_equal(rt.GEOGRAPHIC.to_projected(coords), coords)
        moved = rt.MERCATOR.to_projected(coords)
        assert moved[0][0] > 1e6


class TestGeographicStorage:
    """A geographic pyramid has to be stored one zoom down to fit PMTiles.

    PMTiles orders tiles on a Hilbert curve over a square grid and refuses any
    x past 2**z, so a grid twice as wide as it is tall cannot be written at its
    own zoom. At z+1 the square grid is exactly as wide, and the tiles fill its
    top half.
    """

    def test_geographic_x_would_overflow_its_own_zoom(self):
        widest = max(x for x, _ in rt.GEOGRAPHIC.world_tiles(3))
        assert widest == (1 << 3) * 2 - 1
        assert widest > (1 << 3) - 1, "no overflow, so the offset would be pointless"

    def test_storage_zoom_leaves_room_for_every_tile(self):
        for z in (0, 3, 6):
            for x, y in rt.GEOGRAPHIC.world_tiles(z):
                sz, sx, sy = rt.GEOGRAPHIC.storage_zxy(z, x, y)
                assert sx < (1 << sz) and sy < (1 << sz), f"z{z} {x},{y} -> {sz} {sx},{sy}"

    def test_mercator_is_stored_where_it_is_rendered(self):
        assert rt.MERCATOR.storage_zxy(7, 3, 9) == (7, 3, 9)

    def test_shifted_archive_round_trips_to_the_same_tiles(self, tmp_path):
        """What the shift writes must read back as the tile that was rendered."""
        import sqlite3

        stage = tmp_path / "geo.stage.mbtiles"
        db = rt._open_stage(stage)
        with db:
            for z, x, y in ((0, 0, 0), (0, 1, 0), (2, 5, 1)):
                db.execute(
                    "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                    (z, x, (1 << z) - 1 - y, f"{z}/{x}/{y}".encode()),
                )
        db.close()

        shifted = rt._shift_zooms(stage, rt.GEOGRAPHIC.zoom_offset)
        out = sqlite3.connect(shifted)
        for z, x, y in ((0, 0, 0), (0, 1, 0), (2, 5, 1)):
            sz, sx, sy = rt.GEOGRAPHIC.storage_zxy(z, x, y)
            row = out.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (sz, sx, (1 << sz) - 1 - sy),          # TMS row at the storage zoom
            ).fetchone()
            assert row and row[0] == f"{z}/{x}/{y}".encode(), f"lost {z}/{x}/{y}"
        out.close()
        assert not stage.exists(), "the original staging database should be consumed"


def test_land_is_read_with_cubic_not_nearest():
    """Nearest scores more texture by undersampling, not by keeping detail.

    Every land zoom downsamples -- at z11 the tile pixel is about 54 m against
    a 30 m DEM. Measured against the z12 render averaged down, cubic has the
    lowest error; nearest produces more texture than the data supports and the
    worst error of the four filters tried, so the texture number alone would
    have picked the wrong one.
    """
    import inspect

    from basemap.pipeline import hypsometric

    source = inspect.getsource(hypsometric.specs)
    assert 'resampling="cubic"' in source
    assert 'resampling="nearest"' not in source


def test_the_bathymetry_keeps_the_default_filter():
    """The land finding was measured on land; the sea was not retested."""
    import inspect

    from basemap.pipeline import bathymetry

    assert "resampling=" not in inspect.getsource(bathymetry.specs)
