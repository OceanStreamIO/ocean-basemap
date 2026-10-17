"""The detail zooms: past z12 only where the HR archive has tiles."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from basemap.pipeline import xyz
from basemap.pipeline.xyz import Shard


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(buf, "WEBP")
    return buf.getvalue()


def _archive(tmp_path, tiles):
    """A PMTiles archive holding *tiles* as (z, x, y), through the real finalize."""
    from basemap.processing import raster_tiles as rt

    dst = tmp_path / "bathy_hr.pmtiles"
    stage = rt.stage_path(dst)
    db = rt._open_stage(stage)
    data = _webp()
    db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                   [(z, x, (1 << z) - 1 - y, data) for z, x, y in tiles])
    db.commit()
    db.close()
    rt.finalize(stage, dst, name="bathy_hr")
    return dst


TILES = [(12, 2050, 1450), (13, 4100, 2900), (13, 4100, 2901), (13, 4103, 2950),
         (14, 8200, 5800), (14, 8207, 5901)]


def test_the_detail_zooms_are_nothing_unless_asked():
    assert list(xyz.detail_zooms(12, 0)) == []
    assert list(xyz.detail_zooms(12, 12)) == []
    assert list(xyz.detail_zooms(12, 15)) == [13, 14, 15]


def test_an_archive_lists_exactly_its_tiles_at_a_zoom(tmp_path):
    archive = xyz.Archive(_archive(tmp_path, TILES))
    assert sorted(archive.tiles_at(13)) == [(4100, 2900), (4100, 2901), (4103, 2950)]
    assert sorted(archive.tiles_at(14)) == [(8200, 5800), (8207, 5901)]
    assert archive.tiles_at(12) == [(2050, 1450)]
    assert archive.tiles_at(15) == [], "above the archive's own depth"
    assert archive.tiles_at(3) == []


def test_detail_columns_group_rows_by_column_and_follow_the_shard(tmp_path):
    archive = xyz.Archive(_archive(tmp_path, TILES))
    assert xyz.detail_columns(archive, 13) == {4100: [2900, 2901], 4103: [2950]}
    assert xyz.detail_columns(archive, 13, Shard(0, 2)) == {4100: [2900, 2901]}
    assert xyz.detail_columns(archive, 13, Shard(1, 2)) == {4103: [2950]}
    assert xyz.detail_columns(archive, 15) == {}


def test_the_detail_archive_is_required_when_the_zooms_are_asked_for(tmp_path):
    with pytest.raises(SystemExit, match="HR archive"):
        xyz.detail_archive(tmp_path)


def test_a_batch_renders_only_the_rows_it_is_given(tmp_path, monkeypatch):
    drawn = []
    monkeypatch.setattr(xyz.Composite, "open", classmethod(lambda cls, d, n: "parts"))
    monkeypatch.setattr(xyz, "Contours", lambda path: "contours")
    monkeypatch.setattr(xyz, "Water", lambda path: "water")
    def fake_compose(parts, z, x, y, contours, water):
        drawn.append((z, x, y))
        return b"t"

    monkeypatch.setattr(xyz, "compose", fake_compose)
    xyz._WORKER.clear()
    (tmp_path / f"{xyz.CONTOUR_ARCHIVE}.pmtiles").write_bytes(b"")
    out = xyz._render_batch((tmp_path, tmp_path, 13, [5], 0, 0, [7, 9]))
    assert drawn == [(13, 5, 7), (13, 5, 9)]
    assert [(z, x, y) for z, x, y, _ in out] == drawn
    drawn.clear()
    xyz._render_batch((tmp_path, tmp_path, 2, [1], 0, 4))
    assert drawn == [(2, 1, 0), (2, 1, 1), (2, 1, 2), (2, 1, 3)], "whole columns as before"
    xyz._WORKER.clear()


def test_lakes_do_not_vanish_below_the_last_band(tmp_path):
    from basemap.config import LAKE_BANDS

    deepest = LAKE_BANDS[-1][1][1]
    assert xyz._lake_band_file(tmp_path, deepest) == xyz._lake_band_file(tmp_path, deepest + 3)
    assert xyz._lake_band_file(tmp_path, 15).name == "lakes_all.fgb"
    assert xyz._lake_band_file(tmp_path, 1) is None


def test_the_completeness_check_counts_the_detail_tiles(tmp_path, capsys):
    stage = tmp_path / "xyz.stage.mbtiles"
    whole = Shard().expected_tiles(12)
    xyz._check_complete(stage, whole + 5, Shard(), 12, detail=5)
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit, match="incomplete"):
        xyz._check_complete(stage, Shard(0, 2).expected_tiles(12) + 4, Shard(0, 2), 12, detail=5)


def test_the_tree_resumes_from_a_finished_version_without_redrawing_it():
    """z0-12 columns marked done are skipped and only the detail columns run."""
    import inspect

    source = inspect.getsource(xyz.run)
    assert "if (z, x) not in done:" in source
    assert "detail_columns(archive, z, shard)" in source
    assert "_check_complete(stage_path, staged, shard, max_zoom, detail_tiles)" in source


def test_the_job_passes_the_depth_through():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "deploy" / "edito" / "relief-build"
    assert "BASEMAP_XYZ_DETAIL_MAX_ZOOM" in (root / "31-build-job.yaml").read_text()
    assert "__XYZ_DETAIL_MAX_ZOOM__" in (root / "run.sh").read_text()


def test_the_plain_page_knows_v9_goes_deeper():
    import re
    from pathlib import Path

    viewer = Path(__file__).resolve().parent.parent / "src" / "basemap" / "viewer"
    page = (viewer / "xyz.html").read_text()
    assert re.search(r"v9:\s*\{ path: 'v9/xyz', maxzoom: 12, detail: 15", page)
    # Drawn as a second layer, never by raising the base source's maxzoom: see
    # the comment above TREES for what MapLibre does with a missing tile.
    assert "minzoom: TREE.maxzoom + 1" in page and "maxzoom: TREE.detail" in page
    assert "{ id: 'detail', type: 'raster', source: 'detail' }" in page
