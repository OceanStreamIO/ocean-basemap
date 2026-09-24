"""The reference layer: placement, zoom ranking, and the tiles it cuts."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from basemap.pipeline import reference as ref

# --- anchors ---------------------------------------------------------------

def test_point_anchor_is_the_point():
    assert ref._anchor({"type": "Point", "coordinates": [12.5, -3.25]}) == (12.5, -3.25)


def test_line_anchor_is_the_vertex_mean():
    lon, lat = ref._anchor({"type": "LineString",
                            "coordinates": [[10.0, 0.0], [20.0, 10.0]]})
    assert lon == pytest.approx(15.0, abs=1e-6)
    assert lat == pytest.approx(5.0)


def test_anchor_survives_the_antimeridian():
    """A feature split at the dateline must not land on the prime meridian.

    This is the Emperor Trough: parts at +180 and -180, whose arithmetic mean
    is zero, which put its name over central Spain.
    """
    lon, lat = ref._anchor({
        "type": "MultiLineString",
        "coordinates": [[[179.0, 41.0], [180.0, 41.5]],
                        [[-180.0, 41.5], [-179.0, 42.0]]],
    })
    assert abs(lon) > 178.0, f"anchor fell to {lon}, not the dateline"
    assert lat == pytest.approx(41.5, abs=0.5)


def test_empty_geometry_has_no_anchor():
    assert ref._anchor({"type": "LineString", "coordinates": []}) is None
    assert ref._anchor({}) is None


# --- size ranking ----------------------------------------------------------

def test_point_features_have_no_span():
    assert ref._span_m({"type": "Point", "coordinates": [0, 0]}) is None
    assert ref._span_m({"type": "MultiPoint", "coordinates": [[0, 0]]}) is None


def test_span_does_not_wrap_the_planet_at_the_dateline():
    """Longitudes are unwrapped, so a 2-degree feature is not read as 358."""
    span = ref._span_m({"type": "LineString",
                        "coordinates": [[179.0, 0.0], [-179.0, 0.0]]})
    assert span == pytest.approx(2 / 360 * ref.EQUATOR_M, rel=0.01)


def test_a_bigger_feature_appears_sooner():
    small = ref._zoom_for_span(20_000, 0.0)
    large = ref._zoom_for_span(4_000_000, 0.0)
    assert large < small


def test_zoom_for_span_meets_the_pixel_threshold():
    z = ref._zoom_for_span(500_000, 0.0)
    metres_per_px = ref.EQUATOR_M / ref.TILE / (1 << z)
    assert 500_000 / metres_per_px >= ref.MIN_FEATURE_PX
    if z:                                   # and the level before it did not
        assert 500_000 / (metres_per_px * 2) < ref.MIN_FEATURE_PX


def test_type_floors_the_size_ranking():
    """A wide knoll still waits longer than a basin of the same width."""
    geometry = {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 0.0]]}
    data = {"features": [
        {"properties": {"NAME": "A", "TYPE": "Basin"}, "geometry": geometry},
        {"properties": {"NAME": "B", "TYPE": "Knoll"}, "geometry": geometry},
    ]}
    basin, knoll = ref._undersea_labels(data)
    assert basin.min_zoom < knoll.min_zoom


# --- labels ----------------------------------------------------------------

def test_the_generic_term_is_appended_once():
    data = {"features": [
        {"properties": {"NAME": "Charcot", "TYPE": "Seamounts"},
         "geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"properties": {"NAME": "Gorringe Ridge", "TYPE": "Ridge"},
         "geometry": {"type": "Point", "coordinates": [0, 0]}},
    ]}
    texts = [lb.text for lb in ref._undersea_labels(data)]
    assert texts == ["Charcot Seamounts", "Gorringe Ridge"]


def test_nameless_features_are_dropped():
    data = {"features": [{"properties": {"NAME": "  ", "TYPE": "Ridge"},
                          "geometry": {"type": "Point", "coordinates": [0, 0]}}]}
    assert ref._undersea_labels(data) == []


# --- placement -------------------------------------------------------------

def _probe() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGBA", (1, 1)))


def test_labels_below_their_zoom_are_not_placed():
    labels = [ref.Label("Deep", 0.0, 0.0, ref.TIER_MINOR, 8)]
    assert ref.place(labels, 7, _probe()) == []
    assert len(ref.place(labels, 8, _probe())) == 1


def test_overlapping_labels_collide():
    labels = [ref.Label("Alpha Seamount", 0.0, 0.0, ref.TIER_MINOR, 0),
              ref.Label("Beta Seamount", 0.0001, 0.0001, ref.TIER_MINOR, 0)]
    assert len(ref.place(labels, 6, _probe())) == 1


def test_distant_labels_both_survive():
    labels = [ref.Label("Alpha", -60.0, 0.0, ref.TIER_MINOR, 0),
              ref.Label("Beta", 60.0, 0.0, ref.TIER_MINOR, 0)]
    assert len(ref.place(labels, 6, _probe())) == 2


def test_the_earlier_feature_wins_a_collision():
    """Same tier and the same spot: the one visible from further out stays."""
    labels = [ref.Label("Late", 0.0, 0.0, ref.TIER_MINOR, 6),
              ref.Label("Early", 0.0, 0.0, ref.TIER_MINOR, 3)]
    placed = ref.place(labels, 6, _probe())
    assert [p.text for p in placed] == ["Early"]


def test_placement_is_deterministic():
    labels = [ref.Label(f"F{i}", i * 3.0 - 90, i * 2.0 - 30, ref.TIER_MINOR, 0)
              for i in range(40)]
    first = [(p.text, p.x, p.y) for p in ref.place(labels, 5, _probe())]
    second = [(p.text, p.x, p.y) for p in ref.place(list(reversed(labels)), 5, _probe())]
    assert first == second


def test_water_bodies_outrank_undersea_features():
    labels = [ref.Label("Undersea", 0.0, 0.0, ref.TIER_MINOR, 0),
              ref.Label("Sea", 0.0, 0.0, ref.TIER_WATER, 0)]
    # Water bodies are set in tracked capitals, so the text comes back upper.
    assert [p.text for p in ref.place(labels, 6, _probe())] == ["SEA"]


def test_world_xy_matches_the_web_mercator_grid():
    world = ref.TILE << 4
    assert ref._world_xy(-180.0, 0.0, 4)[0] == pytest.approx(0.0)
    assert ref._world_xy(180.0, 0.0, 4)[0] == pytest.approx(world)
    assert ref._world_xy(0.0, 0.0, 4)[1] == pytest.approx(world / 2)


# --- tiles -----------------------------------------------------------------

def test_only_tiles_with_text_are_written():
    placed = ref.place([ref.Label("Lone Ridge", 0.0, 0.0, ref.TIER_MAJOR, 0)], 4, _probe())
    tiles = ref.render_tiles(placed, 4)
    assert tiles
    assert len(tiles) < 4 ** 4


def test_tiles_are_transparent_png_with_ink_in_them():
    placed = ref.place([ref.Label("Lone Ridge", 0.0, 0.0, ref.TIER_MAJOR, 0)], 4, _probe())
    (data,) = list(ref.render_tiles(placed, 4).values())[:1]
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.mode == "RGBA"
    alpha = image.getchannel("A")
    assert alpha.getextrema() == (0, 255), "should be see-through with opaque text"


def test_a_label_on_a_seam_is_drawn_in_both_tiles():
    """Global placement, then cutting: the two halves must agree.

    Per-tile placement is what makes labels repeat or jump at tile edges, so
    this is the property the whole design exists to hold.
    """
    # Sit a long name exactly on a tile boundary at z2.
    world = ref.TILE << 2
    lon = (ref.TILE / world) * 360.0 - 180.0          # x == one tile edge
    placed = ref.place([ref.Label("A Very Long Abyssal Plain", lon, 0.0,
                                  ref.TIER_MAJOR, 0)], 2, _probe())
    assert placed
    tiles = ref.render_tiles(placed, 2)
    touched = sorted(tiles)
    assert len(touched) >= 2, "a name across a seam must reach both tiles"
    for data in tiles.values():
        assert Image.open(io.BytesIO(data)).getbbox() is not None


def test_labels_off_the_top_of_the_world_are_dropped():
    labels = [ref.Label("Polar", 0.0, 85.05, ref.TIER_MAJOR, 0)]
    assert ref.place(labels, 0, _probe()) == []


# --- wiring ----------------------------------------------------------------

def test_the_step_is_runnable_but_parked():
    """Buildable by hand, deliberately absent from a full run."""
    from basemap.cli import FULL_ORDER, STEPS

    assert STEPS["reference"] == "basemap.pipeline.reference"
    assert "reference" not in FULL_ORDER


def test_the_fonts_are_bundled():
    for name in ("Lato-Regular.ttf", "Lato-Bold.ttf"):
        assert (ref.FONT_DIR / name).is_file(), f"{name} must ship with the repo"


def test_tiers_differ_by_size_or_weight_not_only_colour():
    """The map has a colour-blind reader; tier must survive greyscale."""
    styles = [ref.TIER_STYLE[t] for t in (ref.TIER_WATER, ref.TIER_MAJOR, ref.TIER_MINOR)]
    marks = {(size, bold) for size, bold, _, _ in styles}
    assert len(marks) == len(styles)
