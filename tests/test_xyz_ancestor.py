"""Stand-ins from an ancestor tile, and the coastline switch at z6."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from basemap.pipeline import xyz


def _png(rgb, alpha=255) -> bytes:
    a = np.zeros((xyz.TILE, xyz.TILE, 4), dtype=np.uint8)
    a[..., :3] = rgb
    a[..., 3] = alpha
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, "PNG")
    return buf.getvalue()


class Tiles:
    """An archive holding exactly the tiles it is given."""

    def __init__(self, tiles: dict):
        self.tiles = tiles

    def covers(self, z, west, south, east, north):
        return True

    def get(self, z, x, y):
        return self.tiles.get((z, x, y))


SEA = (40, 120, 200)
LAND = (200, 190, 170)


def _pixels(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)


def test_the_switch_is_the_renderers():
    from basemap.processing.raster_tiles import LOW_DETAIL_MAX_ZOOM

    assert xyz.GENERALISED_MAX_ZOOM == LOW_DETAIL_MAX_ZOOM


def test_a_z6_sea_is_not_stretched_over_land_the_detailed_coastline_drew():
    """Afua: Natural Earth calls the channel sea at z6, OSM calls it land at z7+.

    The sea archive holds the z6 tile and nothing deeper; the land archive holds
    the z9 tile. The z9 tile must be the land, not the z6 sea stretched over it.
    """
    parts = xyz.Composite({
        "bathy_global": Tiles({(6, 0, 0): _png(SEA)}),
        "land_relief": Tiles({(9, 0, 0): _png(LAND)}),
    })
    out = _pixels(xyz.compose(parts, 9, 0, 0))
    assert np.abs(out - LAND).max() < 8


def test_a_stand_in_still_comes_from_the_detailed_levels():
    """A z8 sea tile still stands in at z9-z12, as it always did."""
    parts = xyz.Composite({"bathy_global": Tiles({(8, 0, 0): _png(SEA)})})
    out = _pixels(xyz.compose(parts, 10, 0, 0))
    assert np.abs(out - SEA).max() < 8


def test_the_generalised_coastline_is_the_last_resort_not_a_hole():
    """Nothing at any detailed level: the z6 stand-in, rather than no tile."""
    parts = xyz.Composite({"bathy_global": Tiles({(6, 0, 0): _png(SEA)})})
    data = xyz.compose(parts, 9, 0, 0)
    assert data is not None
    assert np.abs(_pixels(data) - SEA).max() < 8


def test_below_the_switch_ancestors_are_used_as_before():
    parts = xyz.Composite({"bathy_global": Tiles({(3, 0, 0): _png(SEA)})})
    out = _pixels(xyz.compose(parts, 5, 0, 0))
    assert np.abs(out - SEA).max() < 8
