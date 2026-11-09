"""Land cover tint and shoreline features, painted over the shaded land.

EMODnet's lowlands are not flat colour: their plains carry a fine variation
that ours lacked, and when the two maps were put side by side at the Camargue,
the Venice lagoon and Ria Formosa the land here read as a single beige sheet.
Judged on the pixels, the ramp and the light were not the difference -- the
land cover was. Forest is darker than pasture, pasture greener than cropland,
a salt pan paler than any of them, and a map that paints all four one colour
looks flat however well it is lit.

So each pixel is tinted by what grows on it, from ESA WorldCover 2021. The
tint sits on luminance and on the blue-yellow axis, which is what a red-green
reader can see: trees darker and bluer, crops lighter and warmer, wetland
bluer still. It is applied at 0.40 of full strength: at full strength the
pattern competed with the relief, and the 0.67 used until 2026-11 still darkened
the forest enough to read as shadow beside EMODnet's clean elevation colours. The
built-up class is neutral on purpose. At
half strength it drew Padova, Mestre and Faro as grey smudges on the plain,
and at a tenth it was still a smudge, so towns take the colour of the land
they stand on and the places layer names them.

Three shoreline features go on after the tint, at a quarter strength: the
tidal flats, salt pans and seasonally wet ground that Global Surface Water
sees under water part of the year, shifted toward a grey-blue; wetland and
mangrove, bluer and a shade darker; and bare ground, which on a coast is sand,
warmer and a shade lighter. At full strength they made Ria Formosa legible and
the Po plain busy; at a quarter the ria still reads as a lagoon behind its
barrier islands and the plain is quiet. The flats are the one that matters:
without them the ria is a few channels in a sandy shore.

The cover is read at its 74 m stand-in, nearest-neighbour, so at z12 a patch
edge is two pixels square. Averaging per class would soften that; it was not
judged, so it is not done.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from basemap.processing.raster_tiles import (
    MERCATOR,
    TILE,
    Projection,
    Source,
    _dataset,
    _intersects,
    read_elevation,
)

# ESA WorldCover 2021 (v200) classes.
CLASSES = {
    10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
    50: "built-up", 60: "bare / sparse vegetation", 70: "snow and ice",
    80: "permanent water", 90: "herbaceous wetland", 95: "mangroves",
    100: "moss and lichen",
}

# Per class at full strength: a luminance scale, a shift along the blue-yellow
# axis (blue up, red down, in levels), and a desaturation share.
TINT: dict[int, tuple[float, float, float]] = {
    10: (0.88, +3, 0.0),
    20: (0.94, 0, 0.0),
    30: (0.99, -3, 0.0),
    40: (1.03, -6, 0.0),
    50: (1.00, 0, 0.0),      # neutral: see the module docstring
    60: (1.02, -4, 0.0),
    70: (1.00, 0, 0.0),      # ice has its own ramp
    80: (1.00, 0, 0.0),      # water is drawn by the water layers
    90: (0.96, +8, 0.0),
    95: (0.88, +3, 0.0),
    100: (0.97, +2, 0.0),
}
TINT_STRENGTH = 0.40

# The shoreline features, and the share of them that is painted.
FEATURE_STRENGTH = 0.25
# Ground that Global Surface Water saw under water in this share of its clear
# observations: wet part of the year, dry the rest. Above the upper bound it
# is coastal water and the compositor paints it as such (COASTAL_WATER_THRESHOLD
# in pipeline/xyz.py); this band is what lies between water and land.
FLATS_OCCURRENCE = (25.0, 65.0)
FLATS_RGB = (184.0, 203.0, 212.0)


def tint(rgb: np.ndarray, cover: np.ndarray, strength: float = TINT_STRENGTH) -> np.ndarray:
    """*rgb* (float, 0..255) tinted by the WorldCover class under each pixel."""
    out = np.asarray(rgb, dtype=np.float32).copy()
    for cls, (scale, shift, desaturate) in TINT.items():
        if (scale, shift, desaturate) == (1.0, 0, 0.0):
            continue
        mask = cover == cls
        if not mask.any():
            continue
        px = out[mask]
        px = px * (1.0 + (scale - 1.0) * strength)
        px[:, 2] += shift * strength
        px[:, 0] -= shift * strength
        if desaturate:
            mean = px.mean(axis=1, keepdims=True)
            px = px + (mean - px) * desaturate * strength
        out[mask] = px
    return np.clip(out, 0, 255)


def features(rgb: np.ndarray, cover: np.ndarray, occurrence: np.ndarray,
             strength: float = FEATURE_STRENGTH) -> np.ndarray:
    """Tidal flats, wetland and sand over the tinted land, at *strength*."""
    out = np.asarray(rgb, dtype=np.float32).copy()
    if strength <= 0:
        return out
    lo, hi = FLATS_OCCURRENCE
    occ = np.nan_to_num(np.asarray(occurrence, dtype=np.float32), nan=0.0)
    flats = (occ >= lo) & (occ < hi)
    if flats.any():
        share = 0.5 * strength
        out[flats] = out[flats] * (1.0 - share) + np.array(FLATS_RGB, np.float32) * share
    marsh = (cover == 90) | (cover == 95)
    if marsh.any():
        out[marsh, 2] += 10 * strength
        out[marsh, 0] -= 8 * strength
        out[marsh] *= 1.0 - 0.06 * strength
    sand = cover == 60
    if sand.any():
        out[sand, 0] += 6 * strength
        out[sand, 2] -= 10 * strength
        out[sand] *= 1.0 + 0.03 * strength
    return np.clip(out, 0, 255)


def read_cover(source: Source, z: int, x: int, y: int,
               proj: Projection = MERCATOR) -> np.ndarray:
    """The cover class under each pixel of a tile, 0 where there is none.

    Nearest-neighbour, from the coarsest stand-in in the source's pyramid that
    is still finer than the tile's pixels -- a class cannot be averaged.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    out = np.zeros((TILE, TILE), dtype=np.uint8)
    if source.bounds and not _intersects(source.bounds, proj.tile_lonlat_bounds(z, x, y)):
        return out
    pixel_deg = proj.pixel_ground_m(z, x, y)[1] / 111320.0
    ds = _dataset(source.at(pixel_deg), pixel_deg)
    reproject(
        source=rasterio.band(ds, 1),
        destination=out,
        src_crs=source.crs or ds.crs,
        src_transform=ds.transform,
        src_nodata=0,
        dst_transform=from_bounds(*proj.tile_bounds(z, x, y), TILE, TILE),
        dst_crs=proj.crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    return out


@dataclass(frozen=True)
class LandCover:
    """What a land render needs to paint the tint and the features.

    *cover* is the WorldCover mosaic (pipeline/worldcover.py) and *water* the
    Global Surface Water occurrence mosaic (pipeline/surface_water.py). Without
    *water* the flats are not painted.
    """

    cover: Source
    water: Source | None = None
    tint_strength: float = TINT_STRENGTH
    feature_strength: float = FEATURE_STRENGTH

    def apply(self, rgb: np.ndarray, z: int, x: int, y: int,
              proj: Projection = MERCATOR) -> np.ndarray:
        cover = read_cover(self.cover, z, x, y, proj)
        out = tint(rgb, cover, self.tint_strength)
        if self.water is None or self.feature_strength <= 0:
            return out
        occurrence, _ = read_elevation((self.water,), z, x, y, buffer=0, proj=proj,
                                       resampling="average")
        return features(out, cover, occurrence, self.feature_strength)
