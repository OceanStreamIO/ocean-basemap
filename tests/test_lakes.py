"""Tests for adding a lake's own bathymetry through lake-sources.json."""

from __future__ import annotations

import json

import fiona
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box, mapping

from basemap.pipeline.lakes import external_sources, surveyed_lakes, to_bed_elevation

LAKE = (13.0, 58.5, 13.4, 58.9)   # a stand-in for Vanern
SCHEMA = {
    "geometry": "Polygon",
    "properties": {
        "name": "str", "area_km2": "float", "depth_avg": "float", "elev_m": "float",
    },
}


def _raster(path, values, bounds=LAKE, crs="EPSG:4326"):
    h, w = values.shape
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1,
                       dtype="float32", crs=crs, transform=from_bounds(*bounds, w, h)) as ds:
        ds.write(values.astype("float32"), 1)
    return path


def _lakes_file(path, name="Vanern", elev=44.0):
    with fiona.open(path, "w", driver="FlatGeobuf", crs="EPSG:4326",
                    schema=SCHEMA, layer="lakes") as sink:
        sink.write({"geometry": mapping(box(*LAKE)),
                    "properties": {"name": name, "area_km2": 5486.0,
                                   "depth_avg": 27.9, "elev_m": elev}})
    return path


class TestToBedElevation:
    def test_depths_become_bed_elevation_below_the_surface(self, tmp_path):
        raw = _raster(tmp_path / "d.tif", np.full((4, 4), 30.0))
        out = to_bed_elevation(raw, tmp_path / "bed.tif",
                               {"file": "d.tif", "depths": True, "surface_m": 44})
        with rasterio.open(out) as ds:
            assert ds.read(1) == pytest.approx(np.full((4, 4), 14.0))   # 44 m - 30 m

    def test_sign_flips_a_grid_stored_the_other_way_up(self, tmp_path):
        raw = _raster(tmp_path / "d.tif", np.full((4, 4), -30.0))
        out = to_bed_elevation(raw, tmp_path / "bed.tif",
                               {"file": "d.tif", "depths": True, "surface_m": 44, "sign": -1})
        with rasterio.open(out) as ds:
            assert ds.read(1) == pytest.approx(np.full((4, 4), 14.0))

    def test_elevations_pass_through(self, tmp_path):
        raw = _raster(tmp_path / "e.tif", np.full((4, 4), 14.0))
        out = to_bed_elevation(raw, tmp_path / "bed.tif", {"file": "e.tif"})
        with rasterio.open(out) as ds:
            assert ds.read(1) == pytest.approx(np.full((4, 4), 14.0))

    def test_depths_without_a_surface_is_refused(self, tmp_path):
        raw = _raster(tmp_path / "d.tif", np.full((4, 4), 30.0))
        with pytest.raises(ValueError, match="surface_m"):
            to_bed_elevation(raw, tmp_path / "bed.tif", {"file": "d.tif", "depths": True})


class TestManifest:
    def test_entries_become_sources_covering_their_lake(self, tmp_path, monkeypatch):
        work = tmp_path / "work"
        (work / "lakes" / "sources").mkdir(parents=True)
        _raster(work / "lakes" / "sources" / "vanern.tif", np.full((8, 8), 20.0))
        manifest = tmp_path / "lake-sources.json"
        manifest.write_text(json.dumps({"lakes": [
            {"name": "Vanern", "file": "vanern.tif", "depths": True, "surface_m": 44},
        ]}))
        monkeypatch.setattr("basemap.pipeline.lakes.LAKE_SOURCES_FILE", manifest)
        sources, names, ids = external_sources(work)
        assert names == {"Vanern"} and not ids
        assert len(sources) == 1
        assert sources[0].bounds == pytest.approx(LAKE)

    def test_a_missing_file_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        manifest = tmp_path / "lake-sources.json"
        manifest.write_text(json.dumps({"lakes": [{"name": "Vanern", "file": "absent.tif"}]}))
        monkeypatch.setattr("basemap.pipeline.lakes.LAKE_SOURCES_FILE", manifest)
        assert external_sources(tmp_path / "work") == ([], set(), set())


class TestSurveyedSelection:
    """GEBCO holds no bed for these lakes -- flat at the lake's own surface."""

    def test_a_flat_lake_is_dropped(self, tmp_path):
        gebco = _raster(tmp_path / "gebco.tif", np.full((16, 16), 44.0))
        out = surveyed_lakes(_lakes_file(tmp_path / "lakes.fgb"), gebco, tmp_path / "out.fgb")
        with fiona.open(out) as got:
            assert list(got) == []

    def test_a_lake_named_in_the_manifest_is_kept(self, tmp_path):
        gebco = _raster(tmp_path / "gebco.tif", np.full((16, 16), 44.0))
        out = surveyed_lakes(_lakes_file(tmp_path / "lakes.fgb"), gebco, tmp_path / "out.fgb",
                             keep_names={"Vanern"})
        with fiona.open(out) as got:
            assert [f["properties"]["name"] for f in got] == ["Vanern"]
