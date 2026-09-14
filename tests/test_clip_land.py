"""Tests for the OSM coastline clip."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from basemap.processing.clip_land import grid_cells, layer_extent, require_osm_land

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
