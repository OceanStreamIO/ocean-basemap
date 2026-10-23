"""The rivers: one selection rule for the tree and the vector archive."""

from __future__ import annotations

import inspect

import pytest

from basemap.pipeline import rivers, xyz


def test_the_bar_never_rises_with_zoom():
    rules = [rivers.selection(z) for z in range(rivers.MIN_ZOOM, 13)]
    assert all(r is not None for r in rules)
    for earlier, later in zip(rules, rules[1:]):
        assert later[0] <= earlier[0] and later[1] <= earlier[1]
    assert rivers.selection(rivers.MIN_ZOOM - 1) is None


def test_either_order_or_discharge_keeps_a_reach():
    assert rivers.wanted(8, 0.0, 10)
    assert rivers.wanted(1, 5000.0, 5), "the lower Nile has few tributaries and a great flow"
    assert not rivers.wanted(2, 1.0, 12)
    assert not rivers.wanted(9, 1e6, 2), "nothing below the first zoom"


def test_the_bands_tile_the_archive_zooms_without_a_gap():
    bands = rivers.bands()
    assert bands[0][0] == rivers.MIN_ZOOM and bands[-1][1] == rivers.MAX_ZOOM
    for (_, hi), (lo, _) in zip(bands, bands[1:]):
        assert lo == hi + 1


def test_the_tree_reads_the_archive_rule():
    assert "selection(" in inspect.getsource(xyz._rivers_in)
    assert "rivers_fgb" in inspect.getsource(xyz.Water.rivers_path)


def test_pens_grow_with_the_river():
    assert xyz.river_pen(8, 0.0) == 2.0
    assert xyz.river_pen(1, 500.0) == 2.0, "discharge counts as much as order"
    assert xyz.river_pen(8, 0.0) > xyz.river_pen(5, 0.0) > xyz.river_pen(1, 0.0) > 0


def _land(path, boxes):
    """A land file of square polygons, as OSM's split land polygons come."""
    import fiona

    schema = {"geometry": "Polygon", "properties": {}}
    with fiona.open(path, "w", driver="FlatGeobuf", schema=schema, crs="EPSG:4326") as dst:
        for w, s, e, n in boxes:
            dst.write({"geometry": {"type": "Polygon",
                                    "coordinates": [[(w, s), (e, s), (e, n), (w, n), (w, s)]]},
                       "properties": {}})
    return path


def test_staging_keeps_only_what_the_deepest_zoom_draws(tmp_path, monkeypatch):
    import fiona

    from basemap.pipeline import merit_rivers

    network = merit_rivers.network_fgb(tmp_path)
    network.parent.mkdir(parents=True)
    with fiona.open(network, "w", driver="FlatGeobuf", schema=merit_rivers.SCHEMA,
                    crs="EPSG:4326") as dst:
        for i, (order, dis) in enumerate([(1, 0.5), (4, 0.1), (2, 25.0), (3, 9.0)]):
            dst.write({"geometry": {"type": "LineString", "coordinates": [(i, 0), (i + 1, 1)]},
                       "properties": {"order": order, "dis": dis, "upa": 0.0,
                                      "width": 0.0, "wet": 0.0}})
    _land(network.parent / "osm_land.fgb", [(-1, -1, 10, 2)])
    out = rivers.stage(tmp_path)
    assert out == rivers.rivers_fgb(tmp_path)
    with fiona.open(out) as src:
        kept = sorted((f["properties"]["order"], f["properties"]["dis"]) for f in src)
    assert kept == [(2, 25.0), (4, pytest.approx(0.1))], "a dry channel is kept for its order"


def test_a_changed_rule_clears_what_the_old_one_staged(tmp_path, monkeypatch):
    network = tmp_path / "land" / "merit_rivers.fgb"
    network.parent.mkdir(parents=True)
    network.write_bytes(b"network")
    monkeypatch.setattr(rivers, "_network", lambda work: network)
    land = network.parent / "osm_land.fgb"
    land.write_bytes(b"land")
    monkeypatch.setattr(rivers, "_land", lambda work: land)
    staged, band = rivers.rivers_fgb(tmp_path), tmp_path / "rivers" / "rivers_z4-5.fgb"
    archive = tmp_path / "tiles" / "rivers.pmtiles"
    for f in (staged, band, archive):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"old")
    assert rivers.clear_stale(tmp_path, tmp_path / "tiles")      # no record yet: cleared
    assert not staged.exists() and not band.exists() and not archive.exists()
    staged.write_bytes(b"new")
    assert not rivers.clear_stale(tmp_path, tmp_path / "tiles")  # same rule: kept
    assert staged.exists()
    monkeypatch.setattr(rivers, "SELECTION", rivers.SELECTION[:-1] + ((10, 4, 5.0),))
    assert rivers.clear_stale(tmp_path, tmp_path / "tiles")      # new rule: cleared
    assert not staged.exists()


def test_rivers_end_at_the_coastline(tmp_path):
    import numpy as np
    from shapely.geometry import LineString

    # Two land squares side by side, as split land polygons; sea north of y=1.
    land = _land(tmp_path / "land.fgb", [(0, 0, 1, 1), (1, 0, 2, 1)])
    lines = np.asarray([
        LineString([(0.2, 0.5), (1.8, 0.5)]),     # crosses the seam, all on land
        LineString([(0.5, 0.5), (0.5, 1.5)]),     # runs out to sea
        LineString([(0.5, 1.2), (1.5, 1.8)]),     # all at sea
    ], dtype=object)
    which, parts = rivers.clip_to_land(lines, land)
    assert which.tolist() == [0, 1]
    assert parts[0].equals(lines[0]), "a seam between land polygons does not cut a river"
    assert parts[1].equals(LineString([(0.5, 0.5), (0.5, 1.0)])), "cut at the shore"


def test_staging_refuses_without_the_traced_network(tmp_path):
    with pytest.raises(SystemExit, match="merit-rivers"):
        rivers.stage(tmp_path)


def test_the_discharge_bars_are_half_hydrorivers():
    for (z, o, d), (hz, ho, hd) in zip(rivers.SELECTION, rivers.HYDRORIVERS_SELECTION, strict=True):
        assert (z, o) == (hz, ho)
        assert d <= hd / 2 and d >= hd / 2.5, "half, rounded to a measured band"


def test_the_tree_reads_its_reaches_through_the_rule(tmp_path):
    import fiona

    from basemap.pipeline.rivers import rivers_fgb

    fgb = rivers_fgb(tmp_path)
    fgb.parent.mkdir(parents=True)
    schema = {"geometry": "LineString", "properties": {"order": "int", "dis": "float"}}
    with fiona.open(fgb, "w", driver="FlatGeobuf", schema=schema, crs="EPSG:4326") as dst:
        for order, dis in [(8, 100.0), (4, 30.0), (2, 1.0)]:
            dst.write({"geometry": {"type": "LineString", "coordinates": [(0.1, 0.1), (0.2, 0.2)]},
                       "properties": {"order": order, "dis": dis}})
    box = (0.0, 0.0, 1.0, 1.0)
    assert len(xyz._rivers_in(str(fgb), box, 5)) == 1
    assert len(xyz._rivers_in(str(fgb), box, 10)) == 2
    assert xyz._rivers_in(str(fgb), box, 3) == []
    assert xyz._rivers_in(str(fgb), (5.0, 5.0, 6.0, 6.0), 12) == []


def test_the_tree_refuses_to_start_without_rivers(tmp_path):
    (tmp_path / "lakes").mkdir()
    with pytest.raises(SystemExit, match="rivers step"):
        xyz.Water(tmp_path)


def test_the_archive_is_skipped_loudly_where_tippecanoe_cannot_run(tmp_path, monkeypatch, capsys):
    """The tree needs the staged reaches, not the archive; the Job has no tippecanoe."""
    monkeypatch.setattr(rivers, "available", lambda: False)
    fgb = tmp_path / "rivers.fgb"
    fgb.touch()
    assert rivers.archive(tmp_path, tmp_path, fgb) is None
    assert "NOT built" in capsys.readouterr().out
    assert not (tmp_path / "rivers.pmtiles").exists()


def test_a_tippecanoe_that_is_found_but_will_not_run_counts_as_absent(monkeypatch):
    import subprocess

    from basemap.utils import tippecanoe

    monkeypatch.setattr(tippecanoe.shutil, "which", lambda tool: "/opt/tippecanoe")
    broken = subprocess.CompletedProcess([], 127, "", "GLIBCXX_3.4.32 not found")
    monkeypatch.setattr(tippecanoe.subprocess, "run", lambda *a, **k: broken)
    assert tippecanoe.available() is False
    monkeypatch.setattr(tippecanoe.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "tippecanoe v2.82.0", ""))
    assert tippecanoe.available() is True


def test_the_archive_is_credited():
    assert "MERIT Hydro" in rivers.ATTRIBUTION and "ODbL 1.0" in rivers.ATTRIBUTION
    assert "G-RUN" in rivers.ATTRIBUTION and "HydroRIVERS" not in rivers.ATTRIBUTION


def test_a_dry_channel_is_dashed_and_the_dashes_cross_corners():
    # 20 px with a corner at 2: dashes over 0-4 (round the corner), 7-11, 14-18.
    dashes = list(xyz._dashes([(0.0, 0.0), (2.0, 0.0), (2.0, 18.0)], 4.0, 3.0))
    assert dashes == [[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)],
                      [(2.0, 5.0), (2.0, 9.0)],
                      [(2.0, 12.0), (2.0, 16.0)]]


def test_a_dry_channel_inks_less_than_a_river():
    import numpy as np
    from shapely.geometry import LineString

    w, s, e, n = xyz._tile_metre_bounds(6, 33, 22)
    line = LineString([(w, (s + n) / 2), (e, (s + n) / 2)])
    base = np.full((xyz.TILE, xyz.TILE, 3), 200, np.uint8)
    wet = xyz._draw_water(base.copy(), [], [(line, 4, 20.0)], 6, 33, 22)
    dry = xyz._draw_water(base.copy(), [], [(line, 4, 0.1)], 6, 33, 22)

    def inked(img):
        return int((np.abs(img.astype(int) - 200).sum(axis=2) > 0).sum())

    assert 0 < inked(dry) < 0.8 * inked(wet)
