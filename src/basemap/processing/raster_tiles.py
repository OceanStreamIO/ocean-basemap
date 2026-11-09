"""Render continuous colour relief, with its hillshade baked in, to PMTiles.

The depth zones are classed polygons cut from a raster, so every edge between
two zones is a staircase on the source grid, and the hillshade that should give
the seabed its shape was a separate layer the opaque fills buried. This module
renders the elevation grids themselves instead: each tile is read straight
from a priority stack of rasters, coloured on a continuous ramp and shaded,
so there are no class boundaries to step and no layer to lose the relief under.

Tiles can carry the sea as their alpha channel. MapLibre cannot clip a raster
to a polygon, and the land relief and satellite imagery beneath have to show up
to the shoreline, so the bathymetry has to stop short of it itself: OSM land is
rasterised for every tile at several times the tile's resolution, and only
pixels clear of it by a pixel are drawn (see sea_alpha). The vector sea mask
beneath fills the strip left along the shore, with the crisp OSM edge.

Work is split into metatiles so the coastline is fetched once per block of
tiles, rendered in worker processes, and staged in an MBTiles database. A
metatile is only marked done in the same transaction that stores its tiles,
so an interrupted build resumes where it stopped and never skips a failure.
"""

from __future__ import annotations

import io
import math
import multiprocessing
import os
import sqlite3
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.progress import Progress

console = Console()

Bbox = tuple[float, float, float, float]

TILE = 256
# Half the width of the Web Mercator world, in metres.
ORIGIN = 20037508.342789244
MAX_LAT = 85.0511287798066
METATILE = 8
# At and below this zoom the coastline for the alpha channel comes from a
# generalised source (RenderSpec.land_path_low). A pixel there is 2.4 km or
# more, and a z0-6 metatile spans a continent: loading OSM land for it took
# 8.8 GiB per worker, enough with 8 workers to get the pod evicted.
LOW_DETAIL_MAX_ZOOM = 6
# Linear supersampling of the land mask: 4 gives 16 samples per pixel, enough
# for an edge with 17 levels of coverage.
MASK_SUPERSAMPLE = 4
# How much the hillshade contrast is raised over ice (see render_tile).
ICE_RELIEF_GAIN = 2.6


# ---------------------------------------------------------------------------
# Tile maths
# ---------------------------------------------------------------------------


def _lat(merc_y: float) -> float:
    return math.degrees(math.atan(math.sinh(merc_y / 6378137.0)))


@dataclass(frozen=True)
class Projection:
    """A tile grid: how an XYZ address becomes ground, and in what units.

    Web Mercator is the web's default and the only thing MapLibre draws, but it
    cannot represent a pole: it stops at 85.05 degrees, which leaves out 6.7%
    of Antarctica -- the pole and much of the interior -- and as much of the
    Arctic Ocean. A geographic grid reaches 90 degrees and is what CesiumJS
    tiles in natively, so the renderer carries both and each archive says which
    it was built on.

    The grid is the square (or, geographic, two squares) from *origin* down,
    halving with each zoom. *columns* is how many tiles span the world at z0:
    one for Mercator's square world, two for geographic's 2:1.
    """

    name: str
    crs: str
    columns: int
    origin_x: float
    origin_y: float
    span: float          # the width of a z0 tile, in this projection's units
    metres: bool         # units are metres (Mercator) rather than degrees
    zoom_offset: int = 0  # see storage_zxy

    def tile_bounds(self, z: int, x: int, y: int) -> Bbox:
        """Projected bounds of an XYZ tile, in this projection's units."""
        span = self.span / (1 << z)
        west = self.origin_x + x * span
        north = self.origin_y - y * span
        return (west, north - span, west + span, north)

    def world_bounds(self) -> Bbox:
        """The whole grid, in this projection's units."""
        return (self.origin_x, self.origin_y - self.span,
                self.origin_x + self.columns * self.span, self.origin_y)

    def tile_lonlat_bounds(self, z: int, x: int, y: int) -> Bbox:
        w, s, e, n = self.tile_bounds(z, x, y)
        if not self.metres:
            return (w, s, e, n)
        return (w / ORIGIN * 180.0, _lat(s), e / ORIGIN * 180.0, _lat(n))

    def lonlat_to_tile(self, lon: float, lat: float, z: int) -> tuple[int, int]:
        """The XYZ tile containing a point, clamped to the world."""
        n = 1 << z
        if self.metres:
            lat = max(-MAX_LAT, min(MAX_LAT, lat))
            x = int((lon + 180.0) / 360.0 * n * self.columns)
            y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        else:
            x = int((lon - self.origin_x) / self.span * n)
            y = int((self.origin_y - lat) / self.span * n)
        return (
            min(max(x, 0), n * self.columns - 1),
            min(max(y, 0), n * self.rows - 1),
        )

    @property
    def rows(self) -> int:
        """Tiles from north to south at z0. Both grids are one tile tall."""
        return 1

    def tiles_in_bbox(self, bbox: Bbox, z: int) -> Iterable[tuple[int, int]]:
        """Every tile at *z* that overlaps a longitude/latitude box."""
        west, south, east, north = bbox
        x0, y0 = self.lonlat_to_tile(west, north, z)
        x1, y1 = self.lonlat_to_tile(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield x, y

    def world_tiles(self, z: int) -> Iterable[tuple[int, int]]:
        """Every tile at *z*."""
        for x in range(self.columns << z):
            for y in range(self.rows << z):
                yield x, y

    def to_projected(self, coords: np.ndarray) -> np.ndarray:
        """Longitude/latitude to this projection's units, for a whole array."""
        if not self.metres:
            return coords
        out = np.empty_like(coords)
        out[:, 0] = coords[:, 0] * (ORIGIN / 180.0)
        lat = np.radians(np.clip(coords[:, 1], -MAX_LAT, MAX_LAT))
        out[:, 1] = 6378137.0 * np.arcsinh(np.tan(lat))
        return out

    def storage_zxy(self, z: int, x: int, y: int) -> tuple[int, int, int]:
        """This tile's address inside a PMTiles archive.

        PMTiles orders tiles along a Hilbert curve over a square grid and
        rejects any x or y at or past 2**z, so a geographic pyramid -- twice as
        wide as it is tall -- cannot be stored at its own zoom. It fits exactly
        one zoom down: at storage zoom z+1 the grid is 2**(z+1) square, which
        is precisely the geographic grid's width at z, and its 2**z rows fill
        the top half. So the archive holds z+1 and a reader subtracts one,
        which is what *zoom_offset* records in the metadata.
        """
        return (z + self.zoom_offset, x, y)

    def pixel_ground_m(self, z: int, x: int, y: int) -> tuple[float, float]:
        """A pixel's ground size as (north-south, east-west) metres.

        Mercator's pixels are square on the ground, so the hillshade could take
        one number. A geographic pixel is not: its height is fixed and its
        width closes towards the pole, to a thirtieth of its height by 88
        degrees. Shading it as though it were square would read that squeeze as
        a slope and band the whole polar cap.
        """
        w, s, e, n = self.tile_bounds(z, x, y)
        lonlat = self.tile_lonlat_bounds(z, x, y)
        mid = math.cos(math.radians((lonlat[1] + lonlat[3]) / 2))
        if self.metres:
            size = (e - w) / TILE * mid
            return (size, size)
        return ((n - s) / TILE * 111320.0, (e - w) / TILE * 111320.0 * mid)


MERCATOR = Projection(
    name="mercator", crs="EPSG:3857", columns=1,
    origin_x=-ORIGIN, origin_y=ORIGIN, span=2 * ORIGIN, metres=True,
)
# The WMTS/Cesium global-geodetic grid: two square tiles at z0, so a tile stays
# square in degrees and the whole sphere is covered, poles included.
GEOGRAPHIC = Projection(
    name="geographic", crs="EPSG:4326", columns=2,
    origin_x=-180.0, origin_y=90.0, span=180.0, metres=False, zoom_offset=1,
)
PROJECTIONS = {p.name: p for p in (MERCATOR, GEOGRAPHIC)}


# The Web Mercator grid is the default everywhere, so the module keeps its
# plain functions for it; only a geographic render has to name a projection.
def tile_bounds(z: int, x: int, y: int) -> Bbox:
    return MERCATOR.tile_bounds(z, x, y)


def tile_lonlat_bounds(z: int, x: int, y: int) -> Bbox:
    return MERCATOR.tile_lonlat_bounds(z, x, y)


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    return MERCATOR.lonlat_to_tile(lon, lat, z)


def tiles_in_bbox(bbox: Bbox, z: int) -> Iterable[tuple[int, int]]:
    return MERCATOR.tiles_in_bbox(bbox, z)


# ---------------------------------------------------------------------------
# Colour and light
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ramp:
    """A continuous colour ramp: elevations in ascending order, RGB per stop."""

    elevation: tuple[float, ...]
    rgb: tuple[tuple[int, int, int], ...]

    @classmethod
    def parse(cls, text: str) -> Ramp:
        """Read the gdaldem color-relief format: ``elevation R G B`` per line."""
        stops = []
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) < 4 or parts[0].lower() == "nv":
                continue
            stops.append((float(parts[0]), tuple(int(v) for v in parts[1:4])))
        if len(stops) < 2:
            raise ValueError("a ramp needs at least two stops")
        stops.sort()
        return cls(tuple(s[0] for s in stops), tuple(s[1] for s in stops))

    def colour(self, elevation: np.ndarray) -> np.ndarray:
        """Map elevations to float RGB in 0..255, clamping beyond the end stops."""
        xs = np.asarray(self.elevation)
        rgb = np.asarray(self.rgb, dtype=np.float32)
        return np.stack([np.interp(elevation, xs, rgb[:, c]) for c in range(3)], axis=-1)


@dataclass(frozen=True)
class Shading:
    """How strongly a render is hillshaded.

    Relief that reads at z14 vanishes at z6, where a pixel spans kilometres and
    the same slope is a fraction of a degree. The vertical exaggeration grows
    with the pixel size to compensate: ``(pixel_m / reference_m) ** power``,
    clamped to ``[1, max_exaggeration]``.
    """

    strength: float = 0.8
    reference_m: float = 25.0
    power: float = 0.55
    max_exaggeration: float = 40.0
    azimuth: float = 315.0
    altitude: float = 45.0
    # Bounds on the light factor, so steep slopes shade without going black.
    floor: float = 0.5
    ceiling: float = 1.25
    # Below this slope, in degrees on the exaggerated surface, the light fades
    # back to flat: a plain keeps its exact ramp colour instead of carrying the
    # DEM's texture -- field boundaries, levees, the surface model's own noise
    # -- as a grey mottle. Zero leaves every slope lit. Measured on the
    # exaggerated surface rather than the real one so that it means the same
    # thing at every zoom, the way the exaggeration itself does.
    slope_gate_deg: float = 0.0
    # The soft light. Off, the factor is cut hard at floor and ceiling, and a
    # steep slope sits on that limit as a flat grey band. On, the factor eases
    # into both limits and highlights lift toward white instead of multiplying,
    # which keeps every channel in range. *warm* makes a shadow lose blue faster
    # than red, so it deepens the colour under it instead of greying it.
    soft: bool = False
    warm: float = 0.0

    def exaggeration(self, pixel_m: float) -> float:
        return min(max((pixel_m / self.reference_m) ** self.power, 1.0), self.max_exaggeration)


def hillshade(
    dem: np.ndarray, pixel_m: float | tuple[float, float], shading: Shading
) -> np.ndarray:
    """Relative illumination of the inner pixels of a one-pixel-buffered grid.

    Returns 1.0 where the ground is flat, below 1 facing away from the light
    and above it facing towards it. Normalising to flat means a flat seabed
    keeps exactly its ramp colour, which is what the legend shows.
    """
    # One number for a square pixel; north-south and east-west separately for a
    # geographic grid, whose pixels narrow towards the poles.
    dy, dx = (pixel_m, pixel_m) if isinstance(pixel_m, (int, float)) else pixel_m
    z = np.nan_to_num(dem, nan=float(np.nanmean(dem)) if np.isfinite(dem).any() else 0.0)
    z = z * shading.exaggeration(math.sqrt(dy * dx))
    # Rows run north to south, so dz/dy from np.gradient points south.
    dzdy, dzdx = np.gradient(z, dy, dx)
    dzdy = -dzdy
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, -dzdy)
    az = math.radians(shading.azimuth)
    alt = math.radians(shading.altitude)
    lit = math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    lit = lit / math.sin(alt)
    if shading.slope_gate_deg > 0:
        gate = np.clip(np.degrees(slope) / shading.slope_gate_deg, 0.0, 1.0)
        lit = 1.0 + (lit - 1.0) * gate
    return lit[1:-1, 1:-1]


def shade(rgb: np.ndarray, light: np.ndarray, shading: Shading) -> np.ndarray:
    """Apply relative illumination to colours, keeping flat ground unchanged."""
    if not shading.soft:
        factor = np.clip(1.0 + shading.strength * (light - 1.0), shading.floor, shading.ceiling)
        return np.clip(rgb * factor[..., None], 0, 255)
    delta = shading.strength * (light - 1.0)
    depth, lift = 1.0 - shading.floor, shading.ceiling - 1.0
    down = depth * np.tanh(np.maximum(-delta, 0.0) / depth)
    up = lift * np.tanh(np.maximum(delta, 0.0) / lift)
    channels = np.array([1.0 - shading.warm, 1.0, 1.0 + shading.warm], dtype=np.float32)
    out = rgb * np.power((1.0 - down)[..., None], channels)
    return np.clip(out + (255.0 - out) * up[..., None], 0, 255)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One elevation raster in a priority stack.

    *sign* flips grids that store depth positive-down. *crs* overrides a CRS
    the file states badly (the HR netCDF grids declare latitude-first axes).
    Values outside *valid* are treated as gaps, since not every source declares
    its nodata. *bounds* lets a tile skip a source it cannot touch.

    *pyramid* lists coarser stand-ins for *uri* as ``(pixel size in degrees,
    uri)``. A mosaic of thousands of files has no overviews of its own, so a
    low-zoom tile would read every source pixel under it; each tile reads the
    coarsest stand-in that is still finer than its own pixels instead.
    """

    uri: str
    sign: int = 1
    crs: str | None = None
    bounds: Bbox | None = None
    valid: tuple[float, float] = (-12000.0, 9000.0)
    pyramid: tuple[tuple[float, str], ...] = ()
    # The deepest zoom at which this source is a reason to draw a tile. Past
    # it the source still fills the pixels of a tile some finer source is
    # drawing, but on its own it no longer earns one: a 58 m survey rendered
    # to z15 is its own cells stretched fifteen-fold, a megabyte of tiles that
    # say nothing the z12 tile did not. None means no limit. See native_zoom.
    max_zoom: int | None = None

    def at(self, pixel_deg: float) -> str:
        """The URI to read for pixels *pixel_deg* across."""
        fits = [(res, uri) for res, uri in self.pyramid if res <= pixel_deg]
        return max(fits)[1] if fits else self.uri


@dataclass(frozen=True)
class Band:
    """A zoom range and the sources that serve it, highest priority first.

    The first *primary* sources are the reason the band exists; the rest only
    fill around them. A tile the primary sources do not touch is skipped, so an
    archive of HR surveys does not fill up with tiles of the regional grid.
    """

    min_zoom: int
    max_zoom: int
    sources: tuple[Source, ...]
    primary: int = 0


def native_zoom(cell_m: float, lat: float, oversample: int = 1, cap: int = 22) -> int:
    """The deepest Web Mercator zoom a grid of *cell_m* metres earns at *lat*.

    The zoom whose pixel is nearest a cell, plus *oversample* levels: one level
    past native is drawn smoother than the client would stretch it, and is
    where a survey's own texture still reads; beyond that it is the same cells
    magnified. At mid latitudes a 4 m grid earns z15 or more, a 30 m grid z13,
    a 58 m grid z12, GMRT's 100 m z11.
    """
    ground = 2 * math.pi * 6378137.0 * math.cos(math.radians(lat)) / TILE
    native = round(math.log2(ground / max(cell_m, 1e-6)))
    return max(0, min(cap, native + oversample))


def _intersects(a: Bbox, b: Bbox) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# Open datasets, most recently used last. Bounded: an open HR netCDF grid holds
# ~130 MB outside GDAL's block cache (measured), and a worker walking the HR
# pass meets hundreds of them. Unbounded, 53 open grids came to 6.8 GiB in one
# worker and still climbing, and 14 workers overran the build Job's memory.
# With this cap a worker levels off at about 3.5 GiB. Metatiles are rendered in
# spatial order, so the working set is small; the mosaics every tile reads are
# used constantly and so never age out.
MAX_OPEN_DATASETS = 12
_DATASETS: OrderedDict[tuple[str, int], object] = OrderedDict()
_OVERVIEWS: dict[str, tuple[float, list[int]]] = {}


def _dataset(uri: str, pixel_deg: float):
    """Open *uri* at the coarsest overview that is still finer than a pixel.

    Reading a z2 tile from full-resolution GEBCO would pull the whole world
    through the warper; an overview makes it a few megabytes.
    """
    import rasterio

    if uri not in _OVERVIEWS:
        with rasterio.open(uri) as probe:
            _OVERVIEWS[uri] = (abs(probe.res[0]), probe.overviews(1))
    native, factors = _OVERVIEWS[uri]
    level = -1
    for i, factor in enumerate(factors):
        if native * factor <= pixel_deg:
            level = i
    key = (uri, level)
    if key in _DATASETS:
        _DATASETS.move_to_end(key)
        return _DATASETS[key]
    options = {"overview_level": level} if level >= 0 else {}
    _DATASETS[key] = rasterio.open(uri, **options)
    while len(_DATASETS) > MAX_OPEN_DATASETS:
        _, stale = _DATASETS.popitem(last=False)
        stale.close()
    return _DATASETS[key]


def read_elevation(
    sources: Sequence[Source], z: int, x: int, y: int, *,
    buffer: int = 1, primary: int = 0, proj: Projection = MERCATOR,
    resampling: str = "bilinear",
) -> tuple[np.ndarray, bool]:
    """Elevation for a tile plus *buffer* pixels around it, gaps as NaN.

    Also returns whether any of the first *primary* sources supplied a pixel;
    with *primary* 0 that is always True.

    Each source fills only what the ones before it left empty, so a tile over
    the edge of an HR survey is the survey where it exists and the regional
    grid everywhere else -- complete, with nothing underneath to show through.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    size = TILE + 2 * buffer
    w, s, e, n = proj.tile_bounds(z, x, y)
    pixel = (e - w) / TILE
    w, s, e, n = w - buffer * pixel, s - buffer * pixel, e + buffer * pixel, n + buffer * pixel
    # Clip the buffered window to the world before asking the warper for it.
    # On an edge tile the buffer reaches past the antimeridian, and a
    # destination that wraps makes the warper's source-window estimate span the
    # globe: it then resolves the whole tile at world scale across, which comes
    # out as a horizontally smeared strip. It cost the easternmost and
    # westernmost columns of every archive -- New Zealand, Chukotka, west
    # Alaska, Victoria Land -- and measured as a four-fold drop in detail
    # across the tile, not just at its edge. Inner tiles clip nothing and are
    # unchanged, which is what keeps this safe to land mid-pyramid.
    x0, y0, x1, y1 = proj.world_bounds()
    cw, cs, ce, cn = max(w, x0), max(s, y0), min(e, x1), min(n, y1)
    left, top = round((cw - w) / pixel), round((n - cn) / pixel)
    cols, rows = round((ce - cw) / pixel), round((cn - cs) / pixel)
    transform = from_bounds(cw, cs, ce, cn, cols, rows)
    lonlat = proj.tile_lonlat_bounds(z, x, y)
    # A pixel's width in degrees of latitude, which is what the sources are gridded in.
    pixel_deg = proj.pixel_ground_m(z, x, y)[1] / 111320.0

    out = np.full((rows, cols), np.nan, dtype=np.float32)
    hit = primary == 0
    for i, source in enumerate(sources):
        if source.bounds and not _intersects(source.bounds, lonlat):
            continue
        if i >= primary and not hit:
            return out, False
        ds = _dataset(source.at(pixel_deg), pixel_deg)
        part = np.full((rows, cols), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(ds, 1),
            destination=part,
            src_crs=source.crs or ds.crs,
            src_transform=ds.transform,
            src_nodata=ds.nodata,
            dst_transform=transform,
            dst_crs=proj.crs,
            dst_nodata=np.nan,
            resampling=getattr(Resampling, resampling),
        )
        lo, hi = source.valid
        part[(part < lo) | (part > hi)] = np.nan
        part *= source.sign
        gap = np.isnan(out)
        filled = gap & ~np.isnan(part)
        if i < primary and filled.any() and (source.max_zoom is None or z <= source.max_zoom):
            hit = True
        out[filled] = part[filled]
        if not np.isnan(out).any():
            break
    # The halo the clip took back is the one place the world has no next pixel,
    # so repeat the edge: a flat slope across the antimeridian, which is what a
    # seam between two hemispheres of open ocean should look like.
    if (rows, cols) != (size, size):
        out = np.pad(out, ((top, size - rows - top), (left, size - cols - left)),
                     mode="edge")
    return out, hit


# ---------------------------------------------------------------------------
# The coastline mask
# ---------------------------------------------------------------------------


_LAND: dict[str, object] = {}


def _load_whole(path: str):
    """Every geometry in a small file, with a spatial index over them."""
    import fiona
    import shapely
    from shapely.geometry import shape

    with fiona.open(path) as src:
        geoms = np.array([shape(f["geometry"]) for f in src if f["geometry"]], dtype=object)
    return geoms, shapely.STRtree(geoms)


def land_in(land_path: str, lonlat: Bbox, proj: Projection = MERCATOR) -> list:
    """Land within a longitude/latitude box, clipped to it, in Web Mercator.

    A FlatGeobuf (OSM land, 1.3 GB) is queried through its spatial index for
    each box. Anything else is taken to be small (Natural Earth land) and is
    read once per worker and indexed in memory.

    Clipping and reprojection run on whole geometry arrays: a fjord coast holds
    thousands of polygons, and converting them one coordinate at a time in
    Python made the coastline cost more than the rest of the tile put together.
    """
    import fiona
    import shapely
    from shapely.geometry import shape

    w, s, e, n = lonlat
    if land_path.endswith(".fgb"):
        if land_path not in _LAND:
            _LAND[land_path] = fiona.open(land_path)
        src = _LAND[land_path]
        found = [shape(f["geometry"]) for f in src.filter(bbox=(w, s, e, n))]
        geoms = np.array(found, dtype=object)
    else:
        if land_path not in _LAND:
            _LAND[land_path] = _load_whole(land_path)
        every, tree = _LAND[land_path]
        geoms = every[tree.query(shapely.box(w, s, e, n))]
    if not geoms.size:
        return []
    invalid = ~shapely.is_valid(geoms)
    geoms[invalid] = shapely.make_valid(geoms[invalid])
    # Web Mercator cannot represent a pole, and OSM land reaches -90. A
    # geographic grid can, so there it is the full box that is kept.
    limit = MAX_LAT if proj.metres else 90.0
    clip = shapely.box(w, max(s, -limit), e, min(n, limit))
    geoms = shapely.intersection(geoms, clip)
    geoms = geoms[~shapely.is_empty(geoms)]
    return list(shapely.transform(geoms, proj.to_projected))


def polygons_in(
    path: str, lonlat: Bbox, attribute: str, proj: Projection = MERCATOR
) -> list[tuple]:
    """Polygons in a box with one numeric attribute each, in Web Mercator."""
    import fiona
    import shapely
    from shapely.geometry import shape

    key = f"{path}::{attribute}"
    if key not in _LAND:
        _LAND[key] = fiona.open(path)
    w, s, e, n = lonlat
    out = []
    for f in _LAND[key].filter(bbox=(w, s, e, n)):
        value = f["properties"].get(attribute)
        if value is None:
            continue
        out.append((shapely.transform(shape(f["geometry"]), proj.to_projected), float(value)))
    return out


def burn(
    shapes: Sequence[tuple], z: int, x: int, y: int, *,
    buffer: int = 0, proj: Projection = MERCATOR,
) -> np.ndarray:
    """Rasterise polygon attribute values over a tile; NaN where none."""
    from rasterio.features import rasterize
    from rasterio.transform import from_bounds

    w, s, e, n = proj.tile_bounds(z, x, y)
    pad = (e - w) / TILE * buffer
    bounds = (w - pad, s - pad, e + pad, n + pad)
    side = TILE + 2 * buffer
    if not shapes:
        return np.full((side, side), np.nan, dtype=np.float32)
    return rasterize(
        shapes, out_shape=(side, side), transform=from_bounds(*bounds, side, side),
        fill=np.nan, dtype="float32",
    )


def land_fraction(
    land: Sequence, z: int, x: int, y: int, *,
    buffer: int = 0, proj: Projection = MERCATOR,
) -> np.ndarray:
    """Fraction of each pixel that is land, by supersampling.

    With *buffer*, the grid extends that many pixels past the tile on every
    side, so a pixel at the tile's edge can be judged by its neighbours too.
    """
    from rasterio.features import rasterize
    from rasterio.transform import from_bounds
    from shapely import box

    w, s, e, n = proj.tile_bounds(z, x, y)
    pad = (e - w) / TILE * buffer
    bounds = (w - pad, s - pad, e + pad, n + pad)
    side = TILE + 2 * buffer
    area = box(*bounds)
    shapes = [(g, 1) for g in land if g.intersects(area)]
    if not shapes:
        return np.zeros((side, side), dtype=np.float32)
    size = side * MASK_SUPERSAMPLE
    fine = rasterize(
        shapes, out_shape=(size, size), transform=from_bounds(*bounds, size, size),
        fill=0, dtype="uint8",
    )
    k = MASK_SUPERSAMPLE
    return fine.reshape(side, k, side, k).mean(axis=(1, 3), dtype=np.float32)


def inside_alpha(
    polygons: Sequence, z: int, x: int, y: int, proj: Projection = MERCATOR
) -> np.ndarray:
    """Alpha for a tile drawn inside polygons: opaque only well within them.

    The mirror of sea_alpha, for lake beds. A pixel is drawn only when it and
    all eight neighbours are entirely inside a polygon, so an overzoomed tile
    blends within the lake and never spills over its shoreline; the vector lake
    fill beneath covers the strip left around the edge.
    """
    full = land_fraction(polygons, z, x, y, buffer=1, proj=proj) >= 1.0
    inset = np.ones((TILE, TILE), dtype=bool)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            inset &= full[dy:dy + TILE, dx:dx + TILE]
    return np.where(inset, 255.0, 0.0).astype(np.float32)


def water_fraction(
    water: Source, z: int, x: int, y: int, *, buffer: int = 1,
    proj: Projection = MERCATOR,
) -> np.ndarray:
    """Observed water percentage over a tile; 0 where it was never water.

    Outside the dataset's coverage -- above 80N, below 60S -- there are no
    tiles, so the read comes back empty and the answer is "not water", which
    leaves the poles exactly as the coastline had them.
    """
    values, _ = read_elevation([water], z, x, y, buffer=buffer, proj=proj)
    return np.nan_to_num(values, nan=0.0)


def sea_alpha(
    land: Sequence, z: int, x: int, y: int, proj: Projection = MERCATOR,
    water: Source | None = None, water_min: float = 80.0,
) -> np.ndarray:
    """Alpha for the bathymetry: opaque only well clear of any land.

    *water* adds observed surface water back in. The OSM land polygons are cut
    from the OSM coastline, which runs along the outer barrier islands, so a
    lagoon behind them counts as land and is drawn as land at every zoom. The
    depth is not missing -- EMODnet covers the Venice lagoon to -40 m -- only
    the mask. Elevation cannot be the test either: 83% of Flevoland is below
    sea level and it is not the sea. Observed water is the test that separates
    them, since a polder is dry in every scene and a lagoon is wet in all of
    them.

    A pixel is drawn only when it and all eight neighbours hold no land at
    all; everything else is transparent. That keeps the tile's edge off the
    shore however far it is overzoomed: MapLibre blends a stretched tile
    between pixel centres, and an antialiased edge on a z8 tile, stretched to
    z15, spread the sea colour hundreds of metres over the land. Inset like
    this, the blend happens entirely over water, and the strip left along the
    shore is filled by the vector sea mask beneath -- in the shoreline colour,
    with the crisp OSM edge.
    """
    clear = land_fraction(land, z, x, y, buffer=1, proj=proj) == 0
    inset = np.ones((TILE, TILE), dtype=bool)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            inset &= clear[dy:dy + TILE, dx:dx + TILE]

    if water is not None:
        # Observed water is taken whole, without the erosion above. That
        # erosion exists for the *coastline*: an antialiased edge on a z8 tile
        # stretched to z15 spread sea colour hundreds of metres inland, and the
        # vector sea mask fills the strip it leaves. Inland water has no such
        # strip to give away -- eroding it only thins it. Measured on the tile
        # holding the Venice lagoon, the erosion cost 2.9 points of coverage
        # against the 1.7 the mask had just added, so the lagoon came out a
        # ribbon where EMODnet draws a body of water.
        inset |= water_fraction(water, z, x, y, proj=proj)[1:-1, 1:-1] >= water_min
    return np.where(inset, 255.0, 0.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Datum:
    """Polygons whose *attribute* gives the surface elevations are measured from."""

    path: str
    attribute: str


@dataclass(frozen=True)
class RenderSpec:
    """Everything a worker needs to render one archive.

    *mask* is ``"sea"`` to make land transparent (and skip tiles that are all
    land), ``"land"`` to skip tiles with no land but keep them opaque,
    ``"inside"`` to draw only within the mask's polygons (lake beds), or
    ``None`` to render every tile as it is.

    *datum* names polygons carrying the surface elevations are measured
    against, which is subtracted before colouring. GEBCO stores a lake bed as
    its height above sea level, so a lake's depth is its surface elevation
    minus that; without the datum, Lake Superior's bed reads as +24 m.

    *mask_path_low* is the coastline used up to LOW_DETAIL_MAX_ZOOM instead of
    *mask_path*.

    *ice_ramp* colours the pixels inside *ice_path*'s polygons instead of the
    main ramp. Elevation alone cannot tell ice from rock: Greenland's dome
    stands at 2,000-3,200 m, exactly where a hypsometric ramp has run out of
    green and gone to desert brown, and pulling that band towards white would
    take the Tibetan plateau and the Altiplano with it. The ice sheets are
    their own surface, so they get their own ramp and their own mask.

    *clamp* bounds the elevations before shading. A tile is completed from
    whatever its sources hold, and next to an HR survey that is the regional
    grid's land -- a cliff at the coast the hillshade would draw as a dark line.
    The bathymetry clamps to at most 0 and the land to at least 0, so each only
    shades its own side of the shoreline.
    """

    bands: tuple[Band, ...]
    ramp: Ramp
    shading: Shading = field(default_factory=Shading)
    mask: str | None = None
    mask_path: str | None = None
    mask_path_low: str | None = None
    datum: Datum | None = None
    ice_ramp: Ramp | None = None
    ice_path: str | None = None
    projection: Projection = MERCATOR
    quality: int = 85
    clamp: tuple[float, float] = (-math.inf, math.inf)
    # Observed surface water, added to the sea mask so lagoons and estuaries
    # stop being land. See sea_alpha.
    water: Source | None = None
    water_min: float = 80.0
    water_min_zoom: int = 7
    # How the source grid is read when a tile asks for more pixels than it
    # holds. Bilinear smooths, which is right for a 450 m bathymetric grid and
    # wrong for a 30 m surface model, whose speckle -- buildings, field
    # boundaries, tree lines -- is most of what gives flat land its texture.
    resampling: str = "bilinear"
    # Land cover and shoreline features painted over the shaded colour: see
    # processing/landcover.py. Anything with an ``apply(rgb, z, x, y, proj)``.
    land_cover: object | None = None
    # How many zooms deeper a tile is rendered before it is averaged down, as
    # (up to this zoom, levels deeper) in ascending order. One gradient per pixel
    # aliases fine relief into speckle at low zooms; averaging the shaded pixels
    # underneath keeps the ridges and loses the speckle.
    oversample: tuple[tuple[int, int], ...] = ()

    def band(self, z: int) -> Band:
        for band in self.bands:
            if band.min_zoom <= z <= band.max_zoom:
                return band
        raise ValueError(f"no band covers z{z}")

    def levels_deeper(self, z: int) -> int:
        for max_zoom, levels in self.oversample:
            if z <= max_zoom:
                return levels
        return 0

    def deeper_band(self, z: int, levels: int) -> Band:
        """The band a tile at *z* reads when rendered *levels* deeper.

        The deeper level's own sources, so a z7 tile averaged from z8 reads what
        z8 reads, up to the archive's last band: land_relief stops at z10, and
        its z10 tiles read that band's sources at z11's pixels.
        """
        return self.band(min(z + levels, max(b.max_zoom for b in self.bands)))


def _paint(
    spec: RenderSpec,
    z: int,
    x: int,
    y: int,
    band: Band,
    shading: Shading,
    datum: Sequence | None,
    ice: Sequence | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """A tile's colours and the share of each pixel that has data, or None."""
    proj = spec.projection
    dem, hit = read_elevation(band.sources, z, x, y, primary=band.primary, proj=proj,
                              resampling=spec.resampling)
    if not hit:
        return None
    if spec.datum:
        # Depth below the polygon's own surface, not height above sea level.
        dem = dem - burn(datum or [], z, x, y, buffer=1, proj=proj)
    dem = np.clip(dem, *spec.clamp)
    inner = dem[1:-1, 1:-1]
    missing = np.isnan(inner)
    if missing.all():
        return None
    light = hillshade(dem, proj.pixel_ground_m(z, x, y), shading)
    rgb = spec.ramp.colour(np.nan_to_num(inner))
    if spec.ice_ramp is not None and ice:
        frac = land_fraction(ice, z, x, y, proj=proj)
        if frac.any():
            f = frac[..., None]
            rgb = rgb * (1.0 - f) + spec.ice_ramp.colour(np.nan_to_num(inner)) * f
            # Ice is near-featureless beside rock -- the middle of the Greenland
            # sheet falls about a metre per kilometre -- so at the shading that
            # suits terrain the dome comes out as flat white. The relief is
            # stretched about its flat tone rather than shaded a second time:
            # the light is computed once for the tile, and only its contrast is
            # raised, so nothing moves at the ice edge but the colour.
            light = light * (1.0 - frac) + (1.0 + (light - 1.0) * ICE_RELIEF_GAIN) * frac
    rgb = shade(rgb, light, shading)
    if spec.land_cover is not None:
        rgb = spec.land_cover.apply(rgb, z, x, y, proj)
    return rgb, (~missing).astype(np.float32)


def _paint_deeper(
    spec: RenderSpec,
    z: int,
    x: int,
    y: int,
    levels: int,
    datum: Sequence | None,
    ice: Sequence | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """A tile averaged down from its descendants *levels* zooms deeper.

    The exaggeration stays the one this tile would have had. It depends on
    pixel_m / reference_m, so halving both leaves it unchanged; rendering at the
    deeper level's own exaggeration flattens the relief at every zoom it is used.
    """
    n = 1 << levels
    band = spec.deeper_band(z, levels)
    shading = replace(spec.shading, reference_m=spec.shading.reference_m / n)
    colour = np.zeros((TILE * n, TILE * n, 3), dtype=np.float32)
    weight = np.zeros((TILE * n, TILE * n), dtype=np.float32)
    for j in range(n):
        for i in range(n):
            part = _paint(spec, z + levels, x * n + i, y * n + j, band, shading, datum, ice)
            if part is None:
                continue
            rgb, coverage = part
            rows, cols = slice(j * TILE, (j + 1) * TILE), slice(i * TILE, (i + 1) * TILE)
            colour[rows, cols] = rgb * coverage[..., None]
            weight[rows, cols] = coverage
    total = weight.reshape(TILE, n, TILE, n).sum(axis=(1, 3))
    if not total.any():
        return None
    summed = colour.reshape(TILE, n, TILE, n, 3).sum(axis=(1, 3))
    return summed / np.maximum(total, 1.0)[..., None], total / (n * n)


def render_tile(
    spec: RenderSpec,
    z: int,
    x: int,
    y: int,
    land: Sequence | None = None,
    datum: Sequence | None = None,
    ice: Sequence | None = None,
) -> bytes | None:
    """One encoded WebP tile, or None when it would be empty."""
    from PIL import Image

    proj = spec.projection
    alpha = np.full((TILE, TILE), 255.0, dtype=np.float32)
    if spec.mask == "sea":
        alpha = sea_alpha(
            land or [], z, x, y, proj,
            water=spec.water if z >= spec.water_min_zoom else None,
            water_min=spec.water_min,
        )
        if not alpha.any():
            return None  # no pixel clear of land: nothing of the sea to draw
    elif spec.mask == "inside":
        alpha = inside_alpha(land or [], z, x, y, proj)
        if not alpha.any():
            return None  # nothing of this tile lies inside the polygons
    elif spec.mask == "land":
        from shapely import box

        # Only whether land is here at all: the vector sea mask draws the edge.
        tile = box(*proj.tile_bounds(z, x, y))
        if not any(g.intersects(tile) for g in land or []):
            return None

    levels = spec.levels_deeper(z)
    if levels:
        painted = _paint_deeper(spec, z, x, y, levels, datum, ice)
    else:
        painted = _paint(spec, z, x, y, spec.band(z), spec.shading, datum, ice)
    if painted is None:
        return None
    rgb, coverage = painted
    alpha = alpha * coverage
    if not alpha.any():
        return None

    rgba = np.dstack([rgb, alpha]).round().astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=spec.quality, method=4)
    return buf.getvalue()


def _render_metatile(
    spec: RenderSpec, z: int, tiles: tuple[tuple[int, int], ...]
) -> list[tuple[int, int, int, bytes]]:
    land = datum = ice = None
    if spec.mask or spec.datum or spec.ice_path:
        xs = [t[0] for t in tiles]
        ys = [t[1] for t in tiles]
        proj = spec.projection
        w, s, _, _ = proj.tile_lonlat_bounds(z, min(xs), max(ys))
        _, _, e, n = proj.tile_lonlat_bounds(z, max(xs), min(ys))
        # Reach a few pixels past the metatile: the polygons are clipped to
        # this box, and a polygon cut at the edge looks like its own boundary.
        # That trimmed a transparent pixel off every metatile edge inside the
        # lakes, drawing a white grid across them.
        margin = 4 * (360.0 / (proj.columns << z)) / TILE
        w, s, e, n = w - margin, s - margin, e + margin, n + margin
        if spec.mask:
            low = z <= LOW_DETAIL_MAX_ZOOM and spec.mask_path_low
            land = land_in(spec.mask_path_low if low else spec.mask_path, (w, s, e, n), proj)
        if spec.datum:
            datum = polygons_in(spec.datum.path, (w, s, e, n), spec.datum.attribute, proj)
        if spec.ice_path:
            ice = land_in(spec.ice_path, (w, s, e, n), proj)
    out = []
    for x, y in tiles:
        data = render_tile(spec, z, x, y, land, datum, ice)
        if data:
            out.append((z, x, y, data))
    return out


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def metatile_side(z: int) -> int:
    """Tiles along each side of a metatile at *z*.

    The coastline is loaded once per metatile, so a metatile's extent bounds a
    worker's memory. Growing the side with the zoom keeps every metatile within
    one z7 tile, about 2.8 degrees -- the scale of the 2-degree cells the sea
    mask is built on, whose worst case is a few hundred megabytes.
    """
    return max(1, min(METATILE, 1 << max(0, z - 7)))


def metatiles(tiles: Iterable[tuple[int, int, int]]) -> dict[tuple[int, int, int], list]:
    """Group tiles into metatiles, keyed by (z, mx, my).

    The keys depend on metatile_side(); a staging database written with other
    sides must be discarded, or its done markers would name the wrong tiles.
    """
    groups: dict[tuple[int, int, int], list] = {}
    for z, x, y in tiles:
        side = metatile_side(z)
        groups.setdefault((z, x // side, y // side), []).append((x, y))
    return groups


def _open_stage(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER,"
        " tile_row INTEGER, tile_data BLOB,"
        " PRIMARY KEY (zoom_level, tile_column, tile_row));"
        "CREATE TABLE IF NOT EXISTS done (z INTEGER, mx INTEGER, my INTEGER,"
        " PRIMARY KEY (z, mx, my));"
    )
    return db


def render_into(
    stage: Path,
    spec: RenderSpec,
    tiles: Iterable[tuple[int, int, int]],
    *,
    label: str,
    workers: int | None = None,
) -> None:
    """Render *tiles* into the staging MBTiles *stage*, skipping finished work."""
    from basemap.config import RENDER_WORKERS

    workers = workers or RENDER_WORKERS
    groups = metatiles(tiles)
    if not groups:
        return
    db = _open_stage(stage)
    finished = set(db.execute("SELECT z, mx, my FROM done")) & set(groups)
    todo = [(k, tuple(v)) for k, v in sorted(groups.items()) if k not in finished]
    zooms = sorted({k[0] for k in groups})
    console.print(
        f"  Rendering [cyan]{label}[/cyan]: {sum(len(v) for v in groups.values())} tiles "
        f"in {len(groups)} metatiles, z{zooms[0]}-{zooms[-1]}"
        + (f" [yellow](resuming, {len(finished)} done)[/yellow]" if finished else "")
    )

    # GDAL sizes its block cache from the machine's memory, so each worker
    # would claim gigabytes. Spawned workers inherit the environment.
    os.environ.setdefault("GDAL_CACHEMAX", "512")
    # Spawned, not forked: the progress bar runs a thread, and forking a
    # threaded process can leave a worker holding a lock nobody will release.
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
    with Progress(console=console) as progress, pool:
        task = progress.add_task("Metatiles", total=len(groups), completed=len(finished))
        pending: dict = {}
        queue = iter(todo)
        while True:
            # Bounded in flight: finished work is written as it arrives rather
            # than held in memory until the whole pass is over.
            for key, group in queue:
                pending[pool.submit(_render_metatile, spec, key[0], group)] = key
                if len(pending) >= workers * 4:
                    break
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                key = pending.pop(future)
                rows = future.result()
                with db:
                    db.executemany(
                        "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                        [(z, x, (1 << z) - 1 - y, data) for z, x, y, data in rows],
                    )
                    db.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?)", key)
                progress.advance(task)
    db.close()


def staged(stage: Path, z: int) -> set[tuple[int, int]]:
    """The (x, y) of every tile already rendered into *stage* at *z*."""
    if not stage.exists():
        return set()
    db = _open_stage(stage)
    rows = db.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,)
    ).fetchall()
    db.close()
    return {(x, (1 << z) - 1 - row) for x, row in rows}


def finalize(
    stage: Path,
    dst: Path,
    *,
    name: str,
    bounds: Bbox | None = None,
    attribution: str = "",
    projection: Projection = MERCATOR,
) -> Path:
    """Write the staged tiles out as the PMTiles archive *dst*.

    A geographic archive records the grid it was built on. PMTiles has no field
    for a projection -- the format assumes Web Mercator, as every reader of it
    does -- so it goes in the metadata, where a client that knows to look can
    find it and one that does not will at least not be told Mercator.
    """
    from pmtiles.convert import mbtiles_to_pmtiles

    if projection.zoom_offset:
        stage = _shift_zooms(stage, projection.zoom_offset)
    db = _open_stage(stage)
    count, zmin, zmax = db.execute(
        "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles"
    ).fetchone()
    if not count:
        db.close()
        raise RuntimeError(f"{name} rendered no tiles")
    if bounds is None:
        bounds = (-180.0, -90.0, 180.0, 90.0) if not projection.metres else (
            -180.0, -MAX_LAT, 180.0, MAX_LAT
        )
    w, s, e, n = bounds
    with db:
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", [
            ("name", name), ("format", "webp"), ("type", "baselayer"),
            ("minzoom", str(zmin)), ("maxzoom", str(zmax)),
            ("bounds", f"{w},{s},{e},{n}"),
            ("center", f"{(w + e) / 2},{(s + n) / 2},{zmin}"),
            ("attribution", attribution),
            ("crs", projection.crs),
            ("scheme", projection.name),
            ("zoom_offset", str(projection.zoom_offset)),
        ])
    db.close()

    console.print(f"  Writing {count} tiles, z{zmin}-{zmax} → {dst.name}")
    dst.unlink(missing_ok=True)
    mbtiles_to_pmtiles(str(stage), str(dst), None)
    stage.unlink()
    return dst


def _shift_zooms(stage: Path, offset: int) -> Path:
    """Copy a staging database with every tile moved *offset* zooms down.

    A geographic tile is stored at z+1, where the square grid PMTiles insists
    on is exactly as wide as the geographic grid at z (see storage_zxy). The
    rows are the top half of it, and because the staging table holds TMS rows,
    counted from the south, the move adds 2**z to each one.

    Copied rather than updated in place: shifting every row by the same amount
    passes through states where two rows share a primary key, and the original
    is what a rerun resumes from.
    """
    shifted = stage.with_suffix(".shift.mbtiles")
    shifted.unlink(missing_ok=True)
    db = _open_stage(shifted)
    # ATTACH and DETACH cannot run inside a transaction, so the insert commits
    # between them rather than being wrapped with them.
    db.execute("ATTACH DATABASE ? AS src", (str(stage),))
    db.execute(
        "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
        "SELECT zoom_level + ?, tile_column, tile_row + (1 << zoom_level), tile_data "
        "FROM src.tiles",
        (offset,),
    )
    db.commit()
    db.execute("DETACH DATABASE src")
    db.close()
    stage.unlink()
    return shifted


def stage_path(dst: Path) -> Path:
    """Where the tiles for *dst* are staged while it is being built."""
    return dst.with_suffix(".stage.mbtiles")
