"""Tests for processing.clean — seamount property normalization."""

import json
from pathlib import Path

from basemap.processing.clean import clean_seamounts


def test_clean_seamounts(sample_seamounts: Path, tmp_dir: Path):
    output = tmp_dir / "clean.geojson"
    clean_seamounts(sample_seamounts, output)

    assert output.exists()
    with open(output) as f:
        data = json.load(f)

    features = data["features"]
    assert len(features) == 2

    # First feature: NAME field → name
    assert features[0]["properties"]["name"] == "Davidson Seamount"
    assert features[0]["properties"]["type"] == "seamount"

    # Second feature: empty name
    assert features[1]["properties"]["name"] == ""

    # Extra fields should be stripped
    assert "extra_field" not in features[0]["properties"]
    assert "bogus" not in features[1]["properties"]


def test_clean_seamounts_no_geometry(tmp_dir: Path):
    """Features without geometry should be skipped."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": None, "properties": {"NAME": "Ghost"}},
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {"Name": "Real"},
            },
        ],
    }
    src = tmp_dir / "input.geojson"
    src.write_text(json.dumps(geojson))

    output = tmp_dir / "clean.geojson"
    clean_seamounts(src, output)

    with open(output) as f:
        data = json.load(f)

    assert len(data["features"]) == 1
    assert data["features"][0]["properties"]["name"] == "Real"
