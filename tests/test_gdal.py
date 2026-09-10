"""Tests for utils.gdal — GDAL subprocess wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from basemap.utils.gdal import (
    _require,
    _run,
    gdal2tiles,
    gdal_calc,
    gdal_contour,
    gdal_merge,
    gdal_polygonize,
    gdaldem_color_relief,
    gdaldem_hillshade,
    gdalwarp,
    ogr2ogr,
)

# ---------------------------------------------------------------------------
# _require / _run
# ---------------------------------------------------------------------------


def test_require_found():
    with patch("shutil.which", return_value="/usr/bin/gdalinfo"):
        assert _require("gdalinfo") == "/usr/bin/gdalinfo"


def test_require_missing():
    with patch("shutil.which", return_value=None):
        with pytest.raises(SystemExit):
            _require("gdalinfo")


def test_run_success():
    completed = subprocess.CompletedProcess(args=["echo"], returncode=0, stdout="ok", stderr="")
    with patch("subprocess.run", return_value=completed):
        result = _run(["echo", "hello"], label="test")
        assert result.returncode == 0


def test_run_failure():
    completed = subprocess.CompletedProcess(args=["bad"], returncode=1, stdout="", stderr="error")
    with patch("subprocess.run", return_value=completed):
        with pytest.raises(RuntimeError, match="failed with exit code 1"):
            _run(["bad"], label="test")


# ---------------------------------------------------------------------------
# Wrapper argument construction
# ---------------------------------------------------------------------------

_WHICH = "basemap.utils.gdal._require"
_RUN = "basemap.utils.gdal._run"


def _mock_run(completed=None):
    if completed is None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    return patch(_RUN, return_value=completed)


def _mock_require(tool="/usr/bin/tool"):
    return patch(_WHICH, return_value=tool)


class TestGdaldemHillshade:
    def test_multidirectional(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdaldem_hillshade(tmp_path / "in.tif", tmp_path / "out.tif", z_factor=5)
            args = run.call_args[0][0]
            assert "-multidirectional" in args
            assert "-z" in args
            idx = args.index("-z")
            assert args[idx + 1] == "5"

    def test_no_multidirectional(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdaldem_hillshade(
                tmp_path / "in.tif", tmp_path / "out.tif", multidirectional=False,
            )
            args = run.call_args[0][0]
            assert "-multidirectional" not in args


class TestGdaldemColorRelief:
    def test_alpha_flag(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdaldem_color_relief(
                tmp_path / "in.tif", tmp_path / "ramp.txt", tmp_path / "out.tif", alpha=True,
            )
            args = run.call_args[0][0]
            assert "-alpha" in args

    def test_no_alpha(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdaldem_color_relief(
                tmp_path / "in.tif", tmp_path / "ramp.txt", tmp_path / "out.tif",
            )
            args = run.call_args[0][0]
            assert "-alpha" not in args


class TestGdalCalc:
    def test_basic(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal_calc(
                tmp_path / "in.tif", tmp_path / "out.tif",
                calc="A*2", output_type="Float32",
            )
            args = run.call_args[0][0]
            assert any("--calc=A*2" in a for a in args)
            assert any("--type=Float32" in a for a in args)

    def test_nodata(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal_calc(
                tmp_path / "in.tif", tmp_path / "out.tif",
                calc="A", nodata=-9999,
            )
            args = run.call_args[0][0]
            assert any("--NoDataValue=-9999" in a for a in args)


class TestGdalMerge:
    def test_merge_args(self, tmp_path: Path):
        inputs = [tmp_path / "a.tif", tmp_path / "b.tif"]
        with _mock_require(), _mock_run() as run:
            gdal_merge(inputs, tmp_path / "merged.tif")
            args = run.call_args[0][0]
            assert "-o" in args
            assert str(tmp_path / "a.tif") in args
            assert str(tmp_path / "b.tif") in args


class TestGdalwarp:
    def test_resolution(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdalwarp(tmp_path / "in.tif", tmp_path / "out.tif", resolution=0.01, resampling="cubic")
            args = run.call_args[0][0]
            assert "-tr" in args
            idx = args.index("-tr")
            assert args[idx + 1] == "0.01"
            assert "-r" in args
            assert "cubic" in args


class TestGdalPolygonize:
    def test_args(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal_polygonize(
                tmp_path / "in.tif", tmp_path / "out.fgb", layer="zones", field="depth",
            )
            args = run.call_args[0][0]
            assert "zones" in args
            assert "depth" in args
            assert "-of" in args


class TestGdalContour:
    def test_interval(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal_contour(tmp_path / "in.tif", tmp_path / "out.geojson", interval=500)
            args = run.call_args[0][0]
            assert "-i" in args
            idx = args.index("-i")
            assert args[idx + 1] == "500"

    def test_fixed_levels(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal_contour(
                tmp_path / "in.tif", tmp_path / "out.geojson", fixed_levels=[-100, -50],
            )
            args = run.call_args[0][0]
            assert "-fl" in args
            assert "-100" in args
            assert "-50" in args


class TestGdal2tiles:
    def test_args(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            gdal2tiles(
                tmp_path / "in.tif", tmp_path / "tiles", zoom="0-8", processes=4,
            )
            args = run.call_args[0][0]
            assert "--zoom=0-8" in args
            assert "--processes=4" in args
            assert "--xyz" in args


class TestOgr2ogr:
    def test_format_and_where(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            ogr2ogr(
                tmp_path / "in.shp", tmp_path / "out.geojson",
                output_format="GeoJSON", where="depth < 0",
            )
            args = run.call_args[0][0]
            assert "-f" in args
            assert "GeoJSON" in args
            assert "-where" in args
            assert "depth < 0" in args

    def test_t_srs(self, tmp_path: Path):
        with _mock_require(), _mock_run() as run:
            ogr2ogr(
                tmp_path / "in.shp", tmp_path / "out.geojson", t_srs="EPSG:4326",
            )
            args = run.call_args[0][0]
            assert "-t_srs" in args
            assert "EPSG:4326" in args
