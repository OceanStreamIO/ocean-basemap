"""Shared test fixtures for the basemap test suite."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for test output."""
    return tmp_path


@pytest.fixture
def synthetic_dem(tmp_path: Path) -> Path:
    """Create a small synthetic DEM raster (100x100) with ocean and land.

    Values range from -5000 (deep ocean) to 500 (land).
    Ocean is in the left half, land in the right half.
    """
    path = tmp_path / "synthetic_dem.tif"
    width, height = 100, 100
    transform = from_bounds(-180, -90, 180, 90, width, height)

    # Gradient: -5000 on left → +500 on right
    data = np.linspace(-5000, 500, width, dtype=np.float32)
    data = np.tile(data, (height, 1))

    with rasterio.open(
        path, "w",
        driver="GTiff",
        width=width, height=height,
        count=1, dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(data, 1)

    return path


@pytest.fixture
def sample_marine_polys(tmp_path: Path) -> Path:
    """Create a minimal marine polygons GeoJSON."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-180, -60], [180, -60], [180, 60], [-180, 60], [-180, -60]]],
                },
                "properties": {
                    "name": "Pacific Ocean",
                    "name_en": "Pacific Ocean",
                    "featurecla": "ocean",
                    "min_label": 1,
                    "scalerank": 0,
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 50], [30, 50], [30, 70], [0, 70], [0, 50]]],
                },
                "properties": {
                    "name": "North Sea",
                    "name_en": "North Sea",
                    "featurecla": "sea",
                    "min_label": 3,
                    "scalerank": 3,
                },
            },
        ],
    }
    path = tmp_path / "marine_polys.geojson"
    path.write_text(json.dumps(geojson))
    return path


@pytest.fixture
def sample_region_points(tmp_path: Path) -> Path:
    """Create minimal region points GeoJSON."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-30, 40]},
                "properties": {
                    "name": "Mid-Atlantic Ridge",
                    "name_en": "Mid-Atlantic Ridge",
                    "featurecla": "ridge",
                    "min_zoom": 4,
                    "scalerank": 4,
                },
            },
        ],
    }
    path = tmp_path / "region_points.geojson"
    path.write_text(json.dumps(geojson))
    return path


@pytest.fixture
def sample_scufn(tmp_path: Path) -> Path:
    """Create minimal SCUFN gazetteer GeoJSON."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [170, -20]},
                "properties": {
                    "Name": "Tonga Trench",
                    "Generic": "Trench",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [160, -10]},
                "properties": {
                    "Name": "Vanuatu Seamount",
                    "Generic": "Seamount",
                },
            },
        ],
    }
    path = tmp_path / "scufn_features.geojson"
    path.write_text(json.dumps(geojson))
    return path


@pytest.fixture
def sample_seamounts(tmp_path: Path) -> Path:
    """Create minimal seamount GeoJSON (GSFM-like)."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[170, -20], [171, -20], [171, -19], [170, -19], [170, -20]]],
                },
                "properties": {"NAME": "Davidson Seamount", "extra_field": 42},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[160, -10], [161, -10], [161, -9], [160, -9], [160, -10]]],
                },
                "properties": {"name": "", "bogus": "data"},
            },
        ],
    }
    path = tmp_path / "seamounts.geojson"
    path.write_text(json.dumps(geojson))
    return path
