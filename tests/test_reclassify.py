"""Tests for processing.reclassify — depth zone classification."""

from pathlib import Path

import numpy as np
import rasterio

from basemap.config import DEPTH_BREAKS
from basemap.processing.reclassify import reclassify_depth_zones


def test_reclassify_basic(synthetic_dem: Path, tmp_dir: Path):
    """Verify classification assigns correct zone values."""
    output = tmp_dir / "classified.tif"
    reclassify_depth_zones(synthetic_dem, output)

    assert output.exists()
    with rasterio.open(output) as ds:
        data = ds.read(1)
        assert ds.dtypes[0] == "int16"

        # Left side of the raster is deep ocean → should be classified
        # Right side is land → should be 1
        land_pixels = data[:, -1]  # rightmost column (500m elevation)
        assert np.all(land_pixels == 1), "Land pixels should be classified as 1"

        ocean_pixels = data[:, 0]  # leftmost column (-5000m)
        assert np.all(ocean_pixels < 0), "Deep ocean pixels should have negative zone values"


def test_reclassify_zone_values(synthetic_dem: Path, tmp_dir: Path):
    """Zone values should be from DEPTH_BREAKS only (plus 1 for land)."""
    output = tmp_dir / "classified.tif"
    reclassify_depth_zones(synthetic_dem, output)

    with rasterio.open(output) as ds:
        data = ds.read(1)
        valid_values = set(DEPTH_BREAKS) | {1, -32768}
        unique = set(np.unique(data))
        assert unique.issubset(valid_values), f"Unexpected values: {unique - valid_values}"


def test_reclassify_custom_breaks(synthetic_dem: Path, tmp_dir: Path):
    """Custom depth breaks should work."""
    output = tmp_dir / "classified.tif"
    reclassify_depth_zones(synthetic_dem, output, breaks=[-5000, -1000, 0])

    with rasterio.open(output) as ds:
        data = ds.read(1)
        unique = set(np.unique(data))
        valid = {-5000, -1000, 1, -32768}
        assert unique.issubset(valid), f"Got {unique}, expected subset of {valid}"
