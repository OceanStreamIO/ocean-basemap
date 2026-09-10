"""Tests for pipeline.hr_bathymetry — HR index selection, safety and sign detection."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from basemap.pipeline.hr_bathymetry import (
    detect_sign,
    extract_grid,
    geometry_bounds,
    intersects,
    is_trusted_url,
    safe_name,
    select_datasets,
)


def _feature(identifier, url, resolution=64, bbox=(-9.2, 38.4, -9.0, 38.5)):
    west, south, east, north = bbox
    return {
        "properties": {
            "identifier": identifier,
            "resolution": resolution,
            "release": 2018,
            "download_url": url,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ]],
        },
    }


_HOST = "https://downloads.emodnet-bathymetry.eu/high_resolution"


class TestUrlSafety:
    def test_accepts_emodnet_https(self):
        assert is_trusted_url(f"{_HOST}/590_HR_Sesimbra.emo.zip")

    def test_rejects_other_host(self):
        assert not is_trusted_url("https://evil.example.com/x.zip")

    def test_rejects_plain_http(self):
        assert not is_trusted_url("http://downloads.emodnet-bathymetry.eu/x.zip")

    def test_rejects_host_suffix_attack(self):
        assert not is_trusted_url("https://downloads.emodnet-bathymetry.eu.evil.com/x.zip")


class TestSafeName:
    def test_strips_path_and_quote_characters(self):
        assert safe_name("../../etc/passwd") == ".._.._etc_passwd"
        assert safe_name("a' OR 1=1 --") == "a__OR_1_1_--"

    def test_truncates(self):
        assert len(safe_name("x" * 200)) == 80


def test_geometry_bounds():
    geom = {"type": "Polygon", "coordinates": [[[1.0, 2.0], [3.0, 2.0], [3.0, 5.0], [1.0, 5.0]]]}
    assert geometry_bounds(geom) == (1.0, 2.0, 3.0, 5.0)


def test_intersects():
    assert intersects((0, 0, 2, 2), (1, 1, 3, 3))
    assert not intersects((0, 0, 1, 1), (2, 2, 3, 3))


class TestSelectDatasets:
    def test_deduplicates_multipart_footprints(self):
        url = f"{_HOST}/a.zip"
        picked = select_datasets([_feature("A", url), _feature("A", url)])
        assert len(picked) == 1

    def test_filters_by_extent(self):
        picked = select_datasets(
            [_feature("A", f"{_HOST}/a.zip", bbox=(100.0, 10.0, 101.0, 11.0))],
            extent=(-36.0, 15.0, 43.0, 84.0),
        )
        assert picked == []

    def test_filters_coarse_grids(self):
        picked = select_datasets([_feature("A", f"{_HOST}/a.zip", resolution=16)], min_denom=32)
        assert picked == []

    def test_drops_untrusted_urls(self):
        picked = select_datasets([_feature("A", "https://evil.example.com/a.zip")])
        assert picked == []

    def test_sorted_coarsest_first(self):
        picked = select_datasets([
            _feature("fine", f"{_HOST}/fine.zip", resolution=512),
            _feature("coarse", f"{_HOST}/coarse.zip", resolution=32),
        ])
        assert [d.resolution for d in picked] == [32, 512]


def test_extract_grid_ignores_archive_paths(tmp_path: Path):
    archive = tmp_path / "grid.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape/evil.nc", b"netcdf-bytes")
        zf.writestr("readme.txt", b"ignored")

    dest_dir = tmp_path / "grids"
    dest_dir.mkdir()
    result = extract_grid(archive, dest_dir, "safe")

    assert result == dest_dir / "safe.nc"
    assert result.read_bytes() == b"netcdf-bytes"
    assert not (tmp_path.parent / "escape").exists()


def test_extract_grid_without_netcdf(tmp_path: Path):
    archive = tmp_path / "grid.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", b"nothing here")
    assert extract_grid(archive, tmp_path, "x") is None


class _FakeRaster:
    def __init__(self, values):
        self._values = values
        self.width = values.shape[1]
        self.height = values.shape[0]

    def read(self, _band, out_shape=None):
        return self._values

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestDetectSign:
    def test_flips_positive_down_grid(self):
        grid = np.full((8, 8), 12.0, dtype="float32")
        with patch("rasterio.open", return_value=_FakeRaster(grid)), \
                patch("basemap.pipeline.hr_bathymetry._reference_median", return_value=-10.0):
            assert detect_sign("nc", (0, 0, 1, 1)) == -1

    def test_keeps_elevation_convention(self):
        grid = np.full((8, 8), -12.0, dtype="float32")
        with patch("rasterio.open", return_value=_FakeRaster(grid)), \
                patch("basemap.pipeline.hr_bathymetry._reference_median", return_value=-10.0):
            assert detect_sign("nc", (0, 0, 1, 1)) == 1

    def test_defaults_when_reference_unavailable(self):
        grid = np.full((8, 8), 12.0, dtype="float32")
        with patch("rasterio.open", return_value=_FakeRaster(grid)), \
                patch("basemap.pipeline.hr_bathymetry._reference_median", return_value=None):
            assert detect_sign("nc", (0, 0, 1, 1)) == 1

    def test_defaults_when_ambiguous(self):
        grid = np.full((8, 8), 0.2, dtype="float32")
        with patch("rasterio.open", return_value=_FakeRaster(grid)), \
                patch("basemap.pipeline.hr_bathymetry._reference_median", return_value=0.0):
            assert detect_sign("nc", (0, 0, 1, 1)) == 1
