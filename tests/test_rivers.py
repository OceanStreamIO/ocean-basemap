"""HydroRIVERS: one selection rule for the tree and the vector archive."""

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


def test_staging_keeps_only_what_the_deepest_zoom_draws(tmp_path, monkeypatch):
    import fiona

    shp = tmp_path / "src" / "HydroRIVERS_v10.shp"
    shp.parent.mkdir()
    schema = {"geometry": "LineString", "properties": {"ORD_STRA": "int", "DIS_AV_CMS": "float"}}
    with fiona.open(shp, "w", driver="ESRI Shapefile", schema=schema, crs="EPSG:4326") as dst:
        for i, (order, dis) in enumerate([(1, 0.5), (4, 1.0), (2, 25.0), (3, 19.0)]):
            dst.write({"geometry": {"type": "LineString", "coordinates": [(i, 0), (i + 1, 1)]},
                       "properties": {"ORD_STRA": order, "DIS_AV_CMS": dis}})
    monkeypatch.setattr(rivers, "_shapefile", lambda work: shp)
    out = rivers.stage(tmp_path)
    assert out == rivers.rivers_fgb(tmp_path)
    with fiona.open(out) as src:
        kept = sorted((f["properties"]["order"], f["properties"]["dis"]) for f in src)
    assert kept == [(2, 25.0), (4, 1.0)]


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
    fgb = tmp_path / "hydrorivers.fgb"
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
    assert "CC BY 4.0" in rivers.ATTRIBUTION and "HydroRIVERS" in rivers.ATTRIBUTION
