"""Render continuous colour relief, with its hillshade baked in, to PMTiles.

The depth zones are classed polygons cut from a raster, so every edge between
two zones is a staircase on the source grid, and the hillshade that should give
the seabed its shape was a separate layer the opaque fills buried. This module
renders the elevation grids themselves instead: each tile is read straight
from a priority stack of rasters, coloured on a continuous ramp and shaded,
so there are no class boundaries to step and no layer to lose the relief under.

Tiles can carry the sea as their alpha channel. MapLibre cannot clip a raster
to a polygon, and the satellite imagery beneath has to stop at the shoreline,
so the bathymetry has to stop there itself: OSM land is rasterised for every
tile at several times the tile's resolution and averaged down, which lands the
edge on the OSM coastline with antialiasing rather than on the source grid.

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
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Tile maths
# ---------------------------------------------------------------------------


def tile_bounds(z: int, x: int, y: int) -> Bbox:
    """Web Mercator bounds of an XYZ tile, in metres."""
    span = 2 * ORIGIN / (1 << z)
    west = -ORIGIN + x * span
    north = ORIGIN - y * span
    return (west, north - span, west + span, north)


def _lat(merc_y: float) -> float:
    return math.degrees(math.atan(math.sinh(merc_y / 6378137.0)))


def tile_lonlat_bounds(z: int, x: int, y: int) -> Bbox:
    """Longitude/latitude bounds of an XYZ tile."""
    w, s, e, n = tile_bounds(z, x, y)
    return (w / ORIGIN * 180.0, _lat(s), e / ORIGIN * 180.0, _lat(n))


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """The XYZ tile containing a point, clamped to the world."""
    n = 1 << z
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(bbox: Bbox, z: int) -> Iterable[tuple[int, int]]:
    """Every tile at *z* that overlaps a longitude/latitude box."""
    west, south, east, north = bbox
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield x, y


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

    def exaggeration(self, pixel_m: float) -> float:
        return min(max((pixel_m / self.reference_m) ** self.power, 1.0), self.max_exaggeration)


def hillshade(dem: np.ndarray, pixel_m: float, shading: Shading) -> np.ndarray:
    """Relative illumination of the inner pixels of a one-pixel-buffered grid.

    Returns 1.0 where the ground is flat, below 1 facing away from the light
    and above it facing towards it. Normalising to flat means a flat seabed
    keeps exactly its ramp colour, which is what the legend shows.
    """
    z = np.nan_to_num(dem, nan=float(np.nanmean(dem)) if np.isfinite(dem).any() else 0.0)
    z = z * shading.exaggeration(pixel_m)
    # Rows run north to south, so dz/dy from np.gradient points south.
    dzdy, dzdx = np.gradient(z, pixel_m)
    dzdy = -dzdy
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, -dzdy)
    az = math.radians(shading.azimuth)
    alt = math.radians(shading.altitude)
    lit = math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return (lit / math.sin(alt))[1:-1, 1:-1]


def shade(rgb: np.ndarray, light: np.ndarray, shading: Shading) -> np.ndarray:
    """Apply relative illumination to colours, keeping flat ground unchanged."""
    factor = np.clip(1.0 + shading.strength * (light - 1.0), shading.floor, shading.ceiling)
    return np.clip(rgb * factor[..., None], 0, 255)


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
    sources: Sequence[Source], z: int, x: int, y: int, *, buffer: int = 1, primary: int = 0
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
    w, s, e, n = tile_bounds(z, x, y)
    pixel = (e - w) / TILE
    w, s, e, n = w - buffer * pixel, s - buffer * pixel, e + buffer * pixel, n + buffer * pixel
    transform = from_bounds(w, s, e, n, size, size)
    lonlat = tile_lonlat_bounds(z, x, y)
    # A pixel's width in degrees of latitude, which is what the sources are gridded in.
    pixel_deg = pixel * math.cos(math.radians((lonlat[1] + lonlat[3]) / 2)) / 111320.0

    out = np.full((size, size), np.nan, dtype=np.float32)
    hit = primary == 0
    for i, source in enumerate(sources):
        if source.bounds and not _intersects(source.bounds, lonlat):
            continue
        if i >= primary and not hit:
            return out, False
        ds = _dataset(source.at(pixel_deg), pixel_deg)
        part = np.full((size, size), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(ds, 1),
            destination=part,
            src_crs=source.crs or ds.crs,
            src_transform=ds.transform,
            src_nodata=ds.nodata,
            dst_transform=transform,
            dst_crs="EPSG:3857",
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        lo, hi = source.valid
        part[(part < lo) | (part > hi)] = np.nan
        part *= source.sign
        gap = np.isnan(out)
        filled = gap & ~np.isnan(part)
        if i < primary and filled.any():
            hit = True
        out[filled] = part[filled]
        if not np.isnan(out).any():
            break
    return out, hit


# ---------------------------------------------------------------------------
# The coastline mask
# ---------------------------------------------------------------------------


_LAND: dict[str, object] = {}


def _to_mercator(coords: np.ndarray) -> np.ndarray:
    """Longitude/latitude to Web Mercator metres, for a whole coordinate array."""
    out = np.empty_like(coords)
    out[:, 0] = coords[:, 0] * (ORIGIN / 180.0)
    lat = np.radians(np.clip(coords[:, 1], -MAX_LAT, MAX_LAT))
    out[:, 1] = 6378137.0 * np.arcsinh(np.tan(lat))
    return out


def _load_whole(path: str):
    """Every geometry in a small file, with a spatial index over them."""
    import fiona
    import shapely
    from shapely.geometry import shape

    with fiona.open(path) as src:
        geoms = np.array([shape(f["geometry"]) for f in src if f["geometry"]], dtype=object)
    return geoms, shapely.STRtree(geoms)


def land_in(land_path: str, lonlat: Bbox) -> list:
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
    # Web Mercator cannot represent the poles, and OSM land reaches -90.
    clip = shapely.box(w, max(s, -MAX_LAT), e, min(n, MAX_LAT))
    geoms = shapely.intersection(geoms, clip)
    geoms = geoms[~shapely.is_empty(geoms)]
    return list(shapely.transform(geoms, _to_mercator))


def land_fraction(land: Sequence, z: int, x: int, y: int) -> np.ndarray:
    """Fraction of each pixel that is land, antialiased by supersampling."""
    from rasterio.features import rasterize
    from rasterio.transform import from_bounds
    from shapely import box

    bounds = tile_bounds(z, x, y)
    tile = box(*bounds)
    shapes = [(g, 1) for g in land if g.intersects(tile)]
    if not shapes:
        return np.zeros((TILE, TILE), dtype=np.float32)
    size = TILE * MASK_SUPERSAMPLE
    fine = rasterize(
        shapes, out_shape=(size, size), transform=from_bounds(*bounds, size, size),
        fill=0, dtype="uint8",
    )
    k = MASK_SUPERSAMPLE
    return fine.reshape(TILE, k, TILE, k).mean(axis=(1, 3), dtype=np.float32)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderSpec:
    """Everything a worker needs to render one archive.

    *mask* is ``"sea"`` to make land transparent (and skip tiles that are all
    land), ``"land"`` to skip tiles with no land but keep them opaque, or
    ``None`` to render every tile as it is.

    *land_path_low* is the coastline used up to LOW_DETAIL_MAX_ZOOM instead of
    *land_path*.

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
    land_path: str | None = None
    land_path_low: str | None = None
    quality: int = 85
    clamp: tuple[float, float] = (-math.inf, math.inf)

    def band(self, z: int) -> Band:
        for band in self.bands:
            if band.min_zoom <= z <= band.max_zoom:
                return band
        raise ValueError(f"no band covers z{z}")


def render_tile(
    spec: RenderSpec, z: int, x: int, y: int, land: Sequence | None = None
) -> bytes | None:
    """One encoded WebP tile, or None when it would be empty."""
    from PIL import Image

    alpha = np.full((TILE, TILE), 255.0, dtype=np.float32)
    if spec.mask == "sea":
        fraction = land_fraction(land or [], z, x, y)
        if fraction.min() >= 1.0:
            return None  # all land: nothing of the sea to draw
        alpha = (1.0 - fraction) * 255.0
    elif spec.mask == "land":
        from shapely import box

        # Only whether land is here at all: the vector sea mask draws the edge.
        tile = box(*tile_bounds(z, x, y))
        if not any(g.intersects(tile) for g in land or []):
            return None

    band = spec.band(z)
    dem, hit = read_elevation(band.sources, z, x, y, primary=band.primary)
    if not hit:
        return None
    dem = np.clip(dem, *spec.clamp)
    inner = dem[1:-1, 1:-1]
    missing = np.isnan(inner)
    if missing.all():
        return None
    w, _, e, _ = tile_bounds(z, x, y)
    lonlat = tile_lonlat_bounds(z, x, y)
    pixel_m = (e - w) / TILE * math.cos(math.radians((lonlat[1] + lonlat[3]) / 2))

    light = hillshade(dem, pixel_m, spec.shading)
    rgb = shade(spec.ramp.colour(np.nan_to_num(inner)), light, spec.shading)
    alpha[missing] = 0.0
    if not alpha.any():
        return None

    rgba = np.dstack([rgb, alpha]).round().astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=spec.quality, method=4)
    return buf.getvalue()


def _render_metatile(
    spec: RenderSpec, z: int, tiles: tuple[tuple[int, int], ...]
) -> list[tuple[int, int, int, bytes]]:
    land = None
    if spec.mask:
        xs = [t[0] for t in tiles]
        ys = [t[1] for t in tiles]
        w, s, _, _ = tile_lonlat_bounds(z, min(xs), max(ys))
        _, _, e, n = tile_lonlat_bounds(z, max(xs), min(ys))
        low = z <= LOW_DETAIL_MAX_ZOOM and spec.land_path_low
        land = land_in(spec.land_path_low if low else spec.land_path, (w, s, e, n))
    out = []
    for x, y in tiles:
        data = render_tile(spec, z, x, y, land)
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
    bounds: Bbox = (-180.0, -MAX_LAT, 180.0, MAX_LAT),
    attribution: str = "",
) -> Path:
    """Write the staged tiles out as the PMTiles archive *dst*."""
    from pmtiles.convert import mbtiles_to_pmtiles

    db = _open_stage(stage)
    count, zmin, zmax = db.execute(
        "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles"
    ).fetchone()
    if not count:
        db.close()
        raise RuntimeError(f"{name} rendered no tiles")
    w, s, e, n = bounds
    with db:
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", [
            ("name", name), ("format", "webp"), ("type", "baselayer"),
            ("minzoom", str(zmin)), ("maxzoom", str(zmax)),
            ("bounds", f"{w},{s},{e},{n}"),
            ("center", f"{(w + e) / 2},{(s + n) / 2},{zmin}"),
            ("attribution", attribution),
        ])
    db.close()

    console.print(f"  Writing {count} tiles, z{zmin}-{zmax} → {dst.name}")
    dst.unlink(missing_ok=True)
    mbtiles_to_pmtiles(str(stage), str(dst), None)
    stage.unlink()
    return dst


def stage_path(dst: Path) -> Path:
    """Where the tiles for *dst* are staged while it is being built."""
    return dst.with_suffix(".stage.mbtiles")
