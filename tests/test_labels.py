"""Tests for processing.labels — ocean label merging."""

import json
from pathlib import Path

from basemap.processing.labels import (
    merge_ocean_labels,
    process_marine_polys,
    process_region_points,
    process_scufn,
)


def test_process_marine_polys(sample_marine_polys: Path):
    features = process_marine_polys(sample_marine_polys)
    assert len(features) == 2

    pacific = features[0]
    assert pacific["properties"]["name"] == "Pacific Ocean"
    assert pacific["properties"]["label_type"] == "ocean"
    assert pacific["properties"]["text_transform"] == "uppercase"
    assert pacific["geometry"]["type"] == "Point"

    north_sea = features[1]
    assert north_sea["properties"]["label_type"] == "sea"
    assert north_sea["properties"]["min_zoom"] == 3


def test_process_region_points(sample_region_points: Path):
    features = process_region_points(sample_region_points)
    assert len(features) == 1

    ridge = features[0]
    assert ridge["properties"]["name"] == "Mid-Atlantic Ridge"
    assert ridge["properties"]["label_type"] == "ridge"
    assert ridge["properties"]["min_zoom"] == 4


def test_process_scufn(sample_scufn: Path):
    existing = {"pacific ocean"}
    features = process_scufn(sample_scufn, existing)
    assert len(features) == 2

    names = {f["properties"]["name"] for f in features}
    assert "Tonga Trench" in names
    assert "Vanuatu Seamount" in names


def test_scufn_dedup(sample_scufn: Path):
    """SCUFN features with matching names should be deduplicated."""
    existing = {"tonga trench"}
    features = process_scufn(sample_scufn, existing)
    assert len(features) == 1
    assert features[0]["properties"]["name"] == "Vanuatu Seamount"


def test_merge_ocean_labels(
    sample_marine_polys: Path,
    sample_region_points: Path,
    sample_scufn: Path,
    tmp_dir: Path,
):
    output = tmp_dir / "labels.geojson"
    merge_ocean_labels(sample_marine_polys, sample_region_points, sample_scufn, output)

    assert output.exists()
    with open(output) as f:
        data = json.load(f)

    features = data["features"]
    # 2 marine + 1 region + 2 scufn = 5
    assert len(features) == 5

    # All should have required properties
    for feat in features:
        props = feat["properties"]
        assert "name" in props
        assert "min_zoom" in props
        assert "label_type" in props


def test_merge_without_scufn(
    sample_marine_polys: Path,
    sample_region_points: Path,
    tmp_dir: Path,
):
    output = tmp_dir / "labels.geojson"
    merge_ocean_labels(sample_marine_polys, sample_region_points, None, output)

    with open(output) as f:
        data = json.load(f)

    assert len(data["features"]) == 3  # 2 marine + 1 region
