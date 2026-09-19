"""Tests for the OSM coastline clip."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from basemap.processing.clip_land import (
    _sea_cell,
    grid_cells,
    layer_extent,
    require_osm_land,
)

_RUN = "basemap.processing.clip_land.subprocess.run"


class TestGridCells:
    def test_splits_extent_into_cells(self):
        cells = grid_cells((-4, 0, 0, 2), 2.0)
        assert cells == [
            (-4, 0, -2, 2),
            (-2, 0, 0, 2),
        ]

    def test_last_cell_is_truncated_to_the_extent(self):
        cells = grid_cells((0, 0, 3, 1), 2.0)
        assert cells[-1] == (2, 0, 3, 1)
        assert all(c[2] <= 3 and c[3] <= 1 for c in cells)

    def test_whole_world_is_covered_without_gaps(self):
        cells = grid_cells((-180, -90, 180, 90), 2.0)
        assert len(cells) == 180 * 90
        assert cells[0][:2] == (-180, -90)
        assert cells[-1][2:] == (180, 90)

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            grid_cells((0, 0, 1, 1), 0)


class TestLayerExtent:
    def test_parses_ogrinfo_extent(self):
        out = (
            "Layer name: depth_zones\n"
            "Extent: (-180.000000, -90.000000) - (180.000000, 90.000000)\n"
        )
        with patch(_RUN) as run:
            run.return_value.stdout = out
            assert layer_extent("x.fgb", "depth_zones") == (-180.0, -90.0, 180.0, 90.0)

    def test_raises_when_extent_is_absent(self):
        with patch(_RUN) as run:
            run.return_value.stdout = "Layer name: empty\n"
            with pytest.raises(RuntimeError, match="could not read extent"):
                layer_extent("x.fgb", "empty")


class TestRequireOsmLand:
    def test_returns_the_path_when_present(self, tmp_path):
        land = tmp_path / "land" / "osm_land.fgb"
        land.parent.mkdir()
        land.write_bytes(b"")
        assert require_osm_land(tmp_path) == land

    def test_exits_with_guidance_when_missing(self, tmp_path):
        with pytest.raises(SystemExit):
            require_osm_land(tmp_path)


class TestSeaCell:
    """The mask exists to bound satellite imagery along the coast, so a cell
    only earns a polygon when it actually holds some coastline."""

    _WATER = "basemap.processing.clip_land._water_geojson"
    _OGR = "basemap.processing.clip_land._run"

    def test_cell_without_land_is_skipped(self, tmp_path):
        with patch(self._WATER, return_value=False), patch(self._OGR) as run:
            assert _sea_cell(0, (0, 0, 2, 2), tmp_path / "land.fgb", "sea", tmp_path) is None
            run.assert_not_called()
        assert (tmp_path / "done_0").exists()

    def test_cell_that_is_entirely_land_is_skipped(self, tmp_path):
        def _empty(cell, land_src, dest):
            dest.write_text('{"type":"FeatureCollection","features":[]}')
            return True

        with patch(self._WATER, side_effect=_empty), patch(self._OGR) as run:
            assert _sea_cell(1, (0, 0, 2, 2), tmp_path / "land.fgb", "sea", tmp_path) is None
            run.assert_not_called()

    def test_coastal_cell_is_converted(self, tmp_path):
        def _water(cell, land_src, dest):
            dest.write_text(
                '{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":{"type":"Polygon","coordinates":[]}}]}'
            )
            return True

        def _write(args):
            Path(args[args.index("-f") + 2]).touch()

        with patch(self._WATER, side_effect=_water), patch(self._OGR, side_effect=_write) as run:
            part = _sea_cell(2, (0, 0, 2, 2), tmp_path / "land.fgb", "sea", tmp_path)
            assert part == tmp_path / "part_2.fgb"
            assert "sea" in run.call_args[0][0]

    def test_a_finished_cell_is_not_redone(self, tmp_path):
        (tmp_path / "done_3").touch()
        (tmp_path / "part_3.fgb").touch()
        with patch(self._WATER) as water:
            assert _sea_cell(3, (0, 0, 2, 2), tmp_path / "land.fgb", "sea", tmp_path) is not None
            water.assert_not_called()

    def test_a_failed_cell_is_retried_on_resume(self, tmp_path):
        def _water(cell, land_src, dest):
            dest.write_text(
                '{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":{"type":"Polygon","coordinates":[]}}]}'
            )
            return True

        with patch(self._WATER, side_effect=_water), \
             patch(self._OGR, side_effect=RuntimeError("ogr2ogr failed")):
            with pytest.raises(RuntimeError):
                _sea_cell(4, (0, 0, 2, 2), tmp_path / "land.fgb", "sea", tmp_path)
        assert not (tmp_path / "done_4").exists()
