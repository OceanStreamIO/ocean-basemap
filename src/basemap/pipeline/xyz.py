"""Pipeline step 08: the basemap as a plain {z}/{x}/{y} tile tree.

Everything else here is PMTiles: one file per archive, read by range request,
which is what lets the whole map be served from object storage with no tile
server. It costs the reader a library. MapLibre needs three lines; Cesium needs
a provider; GDAL reads our *vector* archives natively but its PMTiles driver is
vector-only, so the raster archives -- the actual basemap -- are exactly the
part a GIS user cannot open.

So the map is published a second way, flattened: one opaque WebP tile per
z/x/y, no alpha, no library, the shape Leaflet, OpenLayers, QGIS and Cesium all
read without being told anything. It is what EMODnet and Esri publish.

    basemap/xyz/{z}/{x}/{y}.webp     z0-12

Composited from the archives rather than re-rendered from the grids. The tiles
already exist -- land_relief to z10, land_relief_eu to z12, bathy_global to z8,
bathy_eu to z11 -- so this is image work, not terrain work, and it cannot drift
from what the PMTiles show because it is made of them.

Past z12 a client stretches the last tile; declare it and every library does
this for you:

    Leaflet     maxNativeZoom: 12, maxZoom: 20
    MapLibre    "maxzoom": 12
    OpenLayers  maxZoom: 12
    Cesium      maximumLevel: 12

That is what EMODnet's deeper tiles are anyway -- theirs shrink with depth,
48 KB at z14 to 20 KB at z16, which is resampling, not detail.
"""

from __future__ import annotations

import functools
import io
import math
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from rich.console import Console
from rich.progress import Progress

from basemap.config import (
    LAKE_DEPTHS_FILE,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
    lake_depth_overrides,
)
from basemap.utils.paths import ensure_dir

console = Console()

# The deepest level the tree is rendered to. Read from the environment because
# MAX_UPSAMPLE below is derived from it, and that derivation has to reach the
# worker processes -- which get the environment, not the CLI argument.
MAX_ZOOM = int(os.environ.get("BASEMAP_XYZ_MAX_ZOOM", "12"))
TILE = 256
WEBP_QUALITY = 85
# method 4 costs about twice method 0 for a percent or two of size; at
# twenty-two million tiles that trade is the wrong way round.
WEBP_METHOD = 1

# The viewer's shore fill (SHORE_WATER in app.js), which has to match the sea
# ramp's surface stop. See color-ramps/bathymetry-water.txt.
SHORE_WATER = (196, 221, 244)

# Which archive supplies each half of the picture, by zoom. Both lists are
# tried in order and the first that has the tile wins, so a zoom the regional
# archive does not reach falls back to the global one.
LAND_BY_ZOOM = [(11, "land_relief_eu"), (0, "land_relief")]
# bathy_eu and bathy_gmrt share a band and are told apart by their headers:
# the reader skips an archive whose declared bounds exclude the tile, so
# Europe comes from EMODnet and the Americas from GMRT with no overlap to
# resolve.
SEA_BY_ZOOM = [(12, "bathy_hr"), (9, "bathy_eu"), (9, "bathy_gmrt"), (0, "bathy_global")]


# Below MAX_ZOOM only where there is something sharper to show. The HR surveys
# are the one source with real pixels past z12 -- bathy_hr is rendered to z15
# inside their footprints -- so with BASEMAP_XYZ_DETAIL_MAX_ZOOM set the tree
# carries z13 up to that zoom for exactly the tiles that archive holds, and
# nothing else. Everywhere else a client overzooms the z12 tile, which is what
# it would get anyway: the land is GLO-30 at about z12 native, the coastal
# water a 30 m grid, the cover 74 m. A uniform z14 was built once (v6) and was
# sixteen times the tiles for pixels that were the z12 ones stretched.
#
# A tile that is not there is therefore the normal case at those zooms, and
# MapLibre keeps showing the parent it already has for a tile that 404s. The
# plain page declares the tree's depth per version (xyz.html).
DETAIL_ARCHIVE = "bathy_hr"
DETAIL_MAX_ZOOM = int(os.environ.get("BASEMAP_XYZ_DETAIL_MAX_ZOOM", "0") or 0)


def detail_zooms(max_zoom: int = MAX_ZOOM, detail_max_zoom: int | None = None) -> range:
    """The zooms past *max_zoom* rendered only where DETAIL_ARCHIVE has tiles."""
    top = DETAIL_MAX_ZOOM if detail_max_zoom is None else detail_max_zoom
    return range(max_zoom + 1, max(top, max_zoom) + 1)


def _pick(table: list[tuple[int, str]], z: int) -> list[str]:
    """Archive names to try at *z*, best first."""
    return [name for lo, name in table if z >= lo]


# Where the staging database lives when nothing says otherwise. Kept as a
# constant because two places name it and they must not drift.
DEFAULT_STAGE_NAME = "xyz.stage.mbtiles"


@dataclass(frozen=True)
class Shard:
    """Which columns one renderer is responsible for.

    ``count == 1`` is the whole tree and is what a local run and the existing
    build Job get, byte for byte. Above one, the tree is split so several hosts
    can render it at once -- twenty-two million tiles is about a hundred
    core-hours, which is an afternoon on sixteen cores and twenty minutes on a
    few hundred.

    The split is by **stride, not by block**. A contiguous run of columns over
    the Pacific is nearly all fast-path open ocean and one over Europe is nearly
    all coastline, contours and lakes, so contiguous shards differ in cost by
    several times and the job waits for the slowest. Every Nth column samples
    the whole world, so every shard costs about the same.

    Striding costs nothing in cache terms. Rendering walks one whole column at a
    time, and Water and Contours key their block caches on (z, x, ...) -- there
    is no locality between neighbouring columns to give up.
    """

    index: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"shard count must be at least 1, not {self.count}")
        if not 0 <= self.index < self.count:
            raise ValueError(f"shard index {self.index} out of range for {self.count} shards")

    @classmethod
    def from_env(cls) -> Shard:
        """The shard this process renders, from BASEMAP_XYZ_SHARD/_SHARDS.

        Unset means the whole tree, so nothing that does not opt in changes.
        """
        count = int(os.environ.get("BASEMAP_XYZ_SHARDS", "1") or 1)
        index = int(os.environ.get("BASEMAP_XYZ_SHARD", "0") or 0)
        return cls(index=index, count=count)

    @property
    def tag(self) -> str | None:
        """Filename infix, or None for the unsharded whole-tree case."""
        return None if self.count == 1 else f"{self.index:03d}of{self.count:03d}"

    def columns(self, z: int) -> range:
        """The columns at *z* that belong to this shard."""
        return range(self.index, 1 << z, self.count)

    def expected_tiles(self, max_zoom: int) -> int:
        """How many tiles this shard must end up holding.

        Exact, not an estimate: compose() returns bytes for every tile in the
        tree, so the whole tree is sum(4**z) and a shard is its stride of it.
        That makes a count comparison a real invariant, which is the only
        cheap defence against a shard that rendered nothing and said nothing.
        """
        return sum(len(self.columns(z)) * (1 << z) for z in range(max_zoom + 1))


def stage_path_for(base: Path, shard: Shard = Shard()) -> Path:
    """Where this shard stages its tiles.

    BASEMAP_XYZ_STAGE wins, because on a cluster the staging database has to go
    on node-local disk: it is opened WAL, and WAL needs a shared-memory mapping
    that network filesystems do not support. Otherwise the database sits beside
    the archives as it always has, with the shard tag in its name so several of
    them can share a directory.
    """
    explicit = os.environ.get("BASEMAP_XYZ_STAGE", "").strip()
    if explicit:
        return Path(explicit)
    name = DEFAULT_STAGE_NAME if shard.tag is None else f"xyz.stage.{shard.tag}.mbtiles"
    return get_tile_dir(base) / name


class Archive:
    """One PMTiles archive, read for a long sequential walk rather than a peek.

    pmtiles.reader.Reader.get() re-reads the header and re-deserialises every
    directory it walks, on every call: measured at 10.8 ms for a hit and
    15.9 ms for a miss. compose() makes up to five lookups a tile, so at
    twenty-two million tiles that alone is the entire cost of the job -- the
    image work beside it is noise. The library is built for reading a handful
    of tiles from an archive, which is not what this does.

    So the header is read once and directories are memoised. Walking in tile
    order means the same leaf answers thousands of consecutive tiles, so the
    cache stays small and nearly always hits.
    """

    __slots__ = ("_source", "_header", "_dirs", "_zoom", "_bounds")

    def __init__(self, path: Path):
        from pmtiles.reader import MmapSource, Reader

        reader = Reader(MmapSource(open(path, "rb")))  # noqa: SIM115
        self._source = reader.get_bytes
        self._header = reader.header()
        self._dirs: dict[tuple[int, int], list] = {}
        self._zoom = (self._header["min_zoom"], self._header["max_zoom"])
        self._bounds = (
            self._header["min_lon_e7"] / 1e7, self._header["min_lat_e7"] / 1e7,
            self._header["max_lon_e7"] / 1e7, self._header["max_lat_e7"] / 1e7,
        )

    def _directory(self, offset: int, length: int):
        from pmtiles.reader import deserialize_directory

        key = (offset, length)
        hit = self._dirs.get(key)
        if hit is None:
            hit = deserialize_directory(self._source(offset, length))
            # Leaves are walked in order, so only a few are ever hot; the cap
            # is there to bound a pathological camera path, not normal use.
            if len(self._dirs) > 512:
                self._dirs.clear()
            self._dirs[key] = hit
        return hit

    def covers(self, z: int, west: float, south: float, east: float, north: float) -> bool:
        """Whether this archive could hold a tile there, from its header alone.

        A miss is the most expensive lookup of all, so the cheapest ones are
        the ones never made: bathy_eu holds nothing in the Pacific and
        bathy_global nothing below z9, and both say so in their header.
        """
        lo, hi = self._zoom
        if not lo <= z <= hi:
            return False
        w, s, e, n = self._bounds
        return not (east < w or west > e or north < s or south > n)

    def tiles_at(self, z: int) -> list[tuple[int, int]]:
        """Every (x, y) this archive holds at *z*, from its directories alone.

        Tile ids are ordered by zoom, so one zoom is one contiguous range of
        ids and the walk skips every leaf outside it without reading it. No
        tile data is touched; three million entries list in seconds.
        """
        from pmtiles.tile import tileid_to_zxy, zxy_to_tileid

        lo, hi = self._zoom
        if not lo <= z <= hi:
            return []
        first, end = zxy_to_tileid(z, 0, 0), zxy_to_tileid(z + 1, 0, 0)
        out: list[tuple[int, int]] = []

        def walk(entries) -> None:
            for i, entry in enumerate(entries):
                if entry.tile_id >= end:
                    return
                if entry.run_length == 0:
                    # A leaf covers the ids up to the next entry's first id.
                    following = entries[i + 1].tile_id if i + 1 < len(entries) else end
                    if following <= first:
                        continue
                    walk(self._directory(
                        self._header["leaf_directory_offset"] + entry.offset, entry.length))
                    continue
                start = max(entry.tile_id, first)
                stop = min(entry.tile_id + entry.run_length, end)
                for tile_id in range(start, stop):
                    _, x, y = tileid_to_zxy(tile_id)
                    out.append((x, y))

        walk(self._directory(self._header["root_offset"], self._header["root_length"]))
        return out

    def get(self, z: int, x: int, y: int) -> bytes | None:
        from pmtiles.reader import find_tile, zxy_to_tileid

        tile_id = zxy_to_tileid(z, x, y)
        offset, length = self._header["root_offset"], self._header["root_length"]
        for _ in range(4):
            found = find_tile(self._directory(offset, length), tile_id)
            if not found:
                return None
            if found.run_length == 0:
                offset = self._header["leaf_directory_offset"] + found.offset
                length = found.length
                continue
            return self._source(self._header["tile_data_offset"] + found.offset, found.length)
        return None


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


# Depth contours, baked in. The PMTiles viewers draw these from the vector
# archive and can turn them off; a plain {z}/{x}/{y} consumer has no overlay
# and no toggle, so for that tree the lines have to be in the pixels. Esri and
# EMODnet both publish their ocean base this way.
#
# depth_contours.pmtiles carries a curated set, not a fixed interval: 200 m
# steps through the deep, the 500 m multiples in between, and a shallow set
# (10, 20, 30, 40, 50, 75, 100, 150, 200 m) for the shelf. Drawing all of it
# at one weight is a thicket; drawing it the way the viewer does -- four
# classes, each fading in at its own zoom -- is the map people already know.
# CONTOUR_CLASSES below is that scheme, copied from app.js stop for stop.
CONTOUR_ARCHIVE = "depth_contours"
CONTOUR_SOURCE_MAX_ZOOM = 10          # the archive's deepest level
CONTOUR_RGB = (66, 106, 148)
# What is drawn at each zoom, and how heavily, is the viewer's own scheme:
# CONTOUR_CLASSES mirrors the four line layers in app.js stop for stop, and
# test_xyz_contours holds the two to each other. The tiles are asked to look
# like the viewer, and the first version of this drew every 500 m multiple at
# one fixed weight from z0 up. At z3 that was forty-five thousand lines through
# one Pacific tile, which the viewer at the same place draws as faint
# thousand-metre lines and nothing else; in the Oslofjord at z11 the shallow
# steps came out solid and stacked where the viewer draws them dashed at a
# third of the weight. Both read as a different, worse map.
#
# Each class carries the viewer's minzoom, its line-width and line-opacity
# stops -- interpolated linearly in zoom and held flat past the last stop, as
# MapLibre does -- and the fraction of a dash period that is ink. A line may
# belong to more than one class, as -50 and -100 do (the shelf layer names
# them and the shallow layer's filter admits them), and gets the coverage the
# two reach together.
CONTOUR_CLASSES = {
    "major":   dict(minzoom=2, width=((2, 0.4), (4, 0.7), (8, 1.2), (10, 1.6)),
                    opacity=((2, 0.20), (4, 0.35), (6, 0.45), (8, 0.50)), dash=1.0),
    "minor":   dict(minzoom=4, width=((3, 0.3), (6, 0.6), (10, 1.0)),
                    opacity=((3, 0.12), (5, 0.25), (7, 0.35), (9, 0.42)), dash=6 / 9),
    "shelf":   dict(minzoom=5, width=((3, 0.2), (5, 0.4), (7, 0.7), (10, 1.0)),
                    opacity=((3, 0.10), (5, 0.20), (7, 0.30), (9, 0.40)), dash=3 / 6),
    "shallow": dict(minzoom=6, width=((6, 0.2), (7, 0.4), (9, 0.6), (10, 0.8)),
                    opacity=((6, 0.10), (7, 0.20), (9, 0.30)), dash=2 / 4),
}
CONTOUR_MIN_ZOOM = min(c["minzoom"] for c in CONTOUR_CLASSES.values())


def contour_classes(depth: int) -> tuple[str, ...]:
    """The viewer layers that draw a contour at *depth*, by app.js's filters."""
    if depth >= 0:
        return ()
    out = []
    if depth % 1000 == 0:
        out.append("major")
    elif depth % 500 == 0:
        out.append("minor")
    if depth > -2000 and ((depth % 200 == 0 and depth % 500 != 0) or depth in (-50, -100)):
        out.append("shelf")
    if depth > -200 and depth % 200 and depth % 500 and depth % 1000:
        out.append("shallow")
    return tuple(out)


def _interpolate(stops, z: float) -> float:
    """MapLibre's linear interpolate, held flat beyond the first and last stop."""
    if z <= stops[0][0]:
        return stops[0][1]
    for (z0, v0), (z1, v1) in zip(stops, stops[1:]):
        if z <= z1:
            return v0 + (v1 - v0) * (z - z0) / (z1 - z0)
    return stops[-1][1]


@functools.lru_cache(maxsize=None)
def contour_style(depth: int, z: int) -> tuple[int, int] | None:
    """(coverage 0-255, pen width in supersampled pixels) for a line, or None.

    Ink is width times opacity times the dashed fraction, which is what the
    viewer puts on screen. The pen is the viewer's width at CONTOUR_SUPERSAMPLE
    and never thinner than one mask pixel; where it had to be rounded, the
    coverage is scaled the other way so the line keeps its ink rather than
    getting heavier.
    """
    ss = CONTOUR_SUPERSAMPLE
    alpha = width = 0.0
    for name in contour_classes(depth):
        spec = CONTOUR_CLASSES[name]
        if z < spec["minzoom"]:
            continue
        coverage = _interpolate(spec["opacity"], z) * spec["dash"]
        alpha = 1 - (1 - alpha) * (1 - coverage)      # one layer over another
        width = max(width, _interpolate(spec["width"], z))
    if alpha <= 0:
        return None
    pen = max(1, round(width * ss))
    return min(255, round(alpha * (width * ss) / pen * 255)), pen


# Rec. 709 luminance of CONTOUR_RGB. The whole difficulty is in this number: it
# was chosen against the pale shelf, where it separates well, and the abyssal
# plain sits at about 84 -- so on open ocean the line and the water were the
# same brightness and the contours all but vanished.
CONTOUR_RGB_LUMINANCE = 101
# How far, in luminance, a contour must sit from the water it crosses, before
# the per-class coverage from contour_style scales it down. Set so that a
# full-coverage deep-water line reaches about the same contrast the
# fixed tone already achieved on the shelf, which is the look nobody objected
# to. Measured, the fixed tone managed four to six on the abyssal plain.
CONTOUR_CONTRAST = 78
# Lines are drawn at 2x and the coverage averaged down: PIL has no
# anti-aliasing, and a hard 1 px line over a soft relief looks drawn on.
CONTOUR_SUPERSAMPLE = 2


class Contours:
    """The contour archive, read as line geometry rather than as tiles.

    Three things make this cheap enough to do twenty million times. The
    geometry is decoded once per *source* tile and reused for all of its
    descendants -- the archive stops at z10, so one decode serves 16 tiles at
    z12 and the walk order means they arrive together. The lines are
    re-projected into each tile rather than upscaled as pixels, so a line is
    one pixel wide at z12 exactly as it is at z10. And every per-point step is
    numpy: a tile on a busy margin carries seventeen thousand points, which in
    a Python comprehension is most of the cost of the whole tile.

    Coordinates come out already multiplied by CONTOUR_SUPERSAMPLE, so the
    drawing code hands them to PIL untouched.
    """

    __slots__ = ("_archive", "_key", "_geom")

    def __init__(self, path: Path):
        self._archive = Archive(path)
        self._key: tuple[int, int, int] | None = None
        self._geom: list[tuple[int, np.ndarray]] = []

    def _decode(self, z: int, x: int, y: int) -> list[tuple[int, np.ndarray]]:
        import gzip

        import mapbox_vector_tile

        if not self._archive.covers(z, *_tile_bounds(z, x, y)):
            return []
        raw = self._archive.get(z, x, y)
        if not raw:
            return []
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        # Everything some viewer layer draws at some zoom is kept. Which of it
        # is drawn, and how heavily, is decided per output tile in lines(),
        # because one decode here serves sixteen tiles at z12.
        out = []
        for layer in mapbox_vector_tile.decode(raw).values():
            extent = layer.get("extent") or 4096
            for feature in layer["features"]:
                depth = feature["properties"].get("depth")
                if depth is None or not contour_classes(int(depth)):
                    continue
                geometry = feature["geometry"]
                parts = ([geometry["coordinates"]] if geometry["type"] == "LineString"
                         else geometry["coordinates"])
                for part in parts:
                    if len(part) < 2:
                        continue
                    line = np.asarray(part, dtype=np.float32) / extent
                    line[:, 1] = 1.0 - line[:, 1]          # MVT counts y up
                    out.append((int(depth), line))
        return out

    def lines(self, z: int, x: int, y: int) -> list[tuple[int, int, np.ndarray]]:
        """Contours crossing this tile as (coverage, pen, points).

        Points are in supersampled tile pixels. Faintest first: the mask keeps
        the last pen over a pixel, so where a major line crosses a shallow one
        the major has to be the one that stays.
        """
        if z < CONTOUR_MIN_ZOOM:
            return []
        source_z = min(z, CONTOUR_SOURCE_MAX_ZOOM)
        up = z - source_z
        key = (source_z, x >> up, y >> up)
        if key != self._key:
            self._key, self._geom = key, self._decode(*key)
        if not self._geom:
            return []

        ss = CONTOUR_SUPERSAMPLE
        span = float((TILE << up) * ss)                    # source tile, at our scale
        origin = np.array([(x - (key[1] << up)) * TILE * ss,
                           (y - (key[2] << up)) * TILE * ss], dtype=np.float32)
        edge = TILE * ss + 2 * ss
        out = []
        for depth, line in self._geom:
            style = contour_style(depth, z)
            if style is None:
                continue
            points = line * span - origin
            lo = points.min(axis=0)
            hi = points.max(axis=0)
            if hi[0] < -2 * ss or lo[0] > edge or hi[1] < -2 * ss or lo[1] > edge:
                continue
            out.append((*style, points))
        out.sort(key=lambda item: item[0])
        return out


@dataclass
class Composite:
    """The archives open for reading, one process's worth."""

    archives: dict

    @classmethod
    def open(cls, tile_dir: Path, names: list[str]) -> Composite:
        return cls({
            name: Archive(tile_dir / f"{name}.pmtiles")
            for name in names
            if (tile_dir / f"{name}.pmtiles").is_file()
        })

    def raw(self, names: list[str], z: int, x: int, y: int) -> bytes | None:
        """The encoded bytes of the first of *names* that holds this tile."""
        box = _tile_bounds(z, x, y)
        for name in names:
            archive = self.archives.get(name)
            if archive is None or not archive.covers(z, *box):
                continue
            data = archive.get(z, x, y)
            if data:
                return data
        return None


# The deepest zoom the global sea archive holds. Over open ocean it is the only
# thing there is, so it sets how far a tile has to reach for a stand-in.
GLOBAL_SEA_MAX_ZOOM = 8

# How far above a tile we will look for something to stretch.
#
# This was a constant 4, which is a 16x magnification and roughly what EMODnet's
# own deepest tiles are. It was also exactly enough by accident: the tree stopped
# at z12 and the global sea archive stops at z8. At z13 four levels reach only
# z9, nothing is there over open ocean, and compose() returns nothing -- which
# cost a whole 16-node render, caught by the tile count and not by the eye.
#
# So it is derived rather than chosen. Deeper zooms do mean more magnification
# out there, and that is honest: past z12 the open ocean is the same GEBCO
# pixels stretched further, and only the line work and the high-resolution
# surveys are really sharper.
MAX_UPSAMPLE = max(4, MAX_ZOOM - GLOBAL_SEA_MAX_ZOOM)


def _from_ancestor(parts: Composite, names: list[str], z: int, x: int, y: int):
    """The tile at z/x/y, or the nearest ancestor scaled up to stand in for it.

    The archives do not tile the world to z12 and were never meant to: GEBCO is
    about 450 m, so bathy_global stops at z8, and land_relief skips tiles with
    no land in them. Above z8 over open ocean neither archive holds anything,
    which left the first run of this step with holes rather than a basemap.

    Stretching the parent is what a client would do with maxNativeZoom anyway,
    and what EMODnet does server-side -- their tiles shrink from 48 KB at z14
    to 20 KB at z16 because there is no new data down there either. Doing it
    here just means the tree has no gaps in it.
    """
    raw = parts.raw(names, z, x, y)
    if raw is not None:
        return Image.open(io.BytesIO(raw)), True

    for up in range(1, MAX_UPSAMPLE + 1):
        za, xa, ya = z - up, x >> up, y >> up
        if za < 0:
            break
        raw = parts.raw(names, za, xa, ya)
        if raw is None:
            continue
        span = TILE >> up                      # the part of the ancestor we want
        left = (x - (xa << up)) * span
        top = (y - (ya << up)) * span
        patch = Image.open(io.BytesIO(raw)).crop((left, top, left + span, top + span))
        return patch.resize((TILE, TILE), Image.BICUBIC), False
    return None, False


def compose(parts: Composite, z: int, x: int, y: int,
            contours: Contours | None = None, water: Water | None = None) -> bytes | None:
    """One opaque tile: land, the shore strip, then the sea over both.

    Three things here are about cost, not correctness, because this runs
    twenty-two million times and the first version of it projected to six days.

    The common case is open ocean: a sea tile that is opaque everywhere and no
    land tile at all. There is nothing to composite, so the archive's own bytes
    go out untouched -- no decode, no blend, no re-encode. Most of the world is
    that case.

    The rest blends in uint8 rather than float32, and dilates with four shifted
    comparisons rather than nine full passes.
    """
    names_land = _pick(LAND_BY_ZOOM, z)
    names_sea = _pick(SEA_BY_ZOOM, z)
    lines = contours.lines(z, x, y) if contours is not None else []

    # The fast path first: a sea tile that exists at this exact zoom, is opaque
    # everywhere, and has no land over it, is already the finished picture. Its
    # bytes go out untouched -- no decode, no blend, no re-encode. Most of the
    # world is that case.
    raw_sea = parts.raw(names_sea, z, x, y)
    if raw_sea is not None and parts.raw(names_land, z, x, y) is None:
        probe = Image.open(io.BytesIO(raw_sea))
        if probe.mode == "RGB" or _is_opaque(probe):
            if not lines:
                return raw_sea
            # Open ocean with a contour through it: the only reason this tile
            # has to be decoded and re-encoded at all.
            return _encode(_draw_contours(
                np.array(probe.convert("RGB"), dtype=np.uint8), lines, None))

    sea_img, _ = _from_ancestor(parts, names_sea, z, x, y)
    land_img, _ = _from_ancestor(parts, names_land, z, x, y)
    if sea_img is None and land_img is None:
        return None

    land = land_img.convert("RGB") if land_img is not None else None
    if sea_img is None:
        # Wholly inland: no coast, no contours, but this is where most of the
        # world's lakes are.
        # Wholly inland: every pixel is the land side.
        return _encode(_paint_water(np.array(land, dtype=np.uint8), water, z, x, y,
                                    parts=parts))

    wet = np.asarray(sea_img.convert("RGBA"))
    alpha = wet[..., 3]
    if land is None:
        base = np.empty((TILE, TILE, 3), dtype=np.uint8)
        base[:] = SHORE_WATER
    else:
        base = np.array(land, dtype=np.uint8)

    # The sea tiles are inset a pixel from the OSM shoreline, so that a coarse
    # archive stretched to a high zoom never tints the land. In the map a
    # vector sea mask fills that strip; here nothing does, and the land colour
    # would show as a hairline along every coast. Growing the sea's own alpha
    # by one pixel covers exactly the strip it left, and the shore colour goes
    # underneath so the grown edge is water rather than whatever the land was.
    grown = _grow(alpha)
    strip = (grown > 0) & (alpha == 0)
    if strip.any():
        base[strip] = SHORE_WATER

    if alpha.min() == 255:
        base = wet[..., :3]
    else:
        a = alpha[..., None].astype(np.uint16)
        base = ((base.astype(np.uint16) * (255 - a) + wet[..., :3].astype(np.uint16) * a)
                // 255).astype(np.uint8)

    if lines:
        base = _draw_contours(base, lines, (grown > 0).astype(np.uint16))

    # Water goes on last, over the sea rather than under it, so a lake keeps
    # its own ramp where the bathymetry would have given it the sea's. The
    # land side is everything the sea archive did not draw, including the
    # shore strip: coastal water is painted there and nowhere else, so the
    # open shelf keeps its bathymetry.
    return _encode(_paint_water(base, water, z, x, y,
                                on_land=(grown == 0).astype(np.float32), parts=parts))


def _encode(pixels: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buf, "WEBP", quality=WEBP_QUALITY,
                                        method=WEBP_METHOD)
    return buf.getvalue()


def _draw_contours(base: np.ndarray, lines, sea: np.ndarray | None) -> np.ndarray:
    """Blend the contour lines into an RGB tile, clipped to the sea.

    Drawn at CONTOUR_SUPERSAMPLE and averaged down, because PIL has no
    anti-aliasing and a hard one-pixel line over soft relief reads as ink on
    top of the map rather than part of it. Averaging the coverage mask, not
    the colour, keeps that to one resize of an 8-bit plane.

    Every class shares a tone and differs only in coverage and pen, so they all
    go into one mask as grey levels and come out in one blend. Reviewed against
    separate colours per class: they differ by under ten in each channel, which
    is invisible under a line a pixel wide, and it is a fraction of the work.

    The tone is taken from the water rather than fixed, and that is the whole
    point of it. A fixed colour was used until it was measured: CONTOUR_RGB sits
    at luminance 100, which reads well against the pale shelf it was chosen on
    and has almost nothing left against the abyssal plain at 84. The line landed
    four to six luminance units from its background there -- invisible, and
    invisible exactly where the contours are the only thing describing the
    shape of the seabed. It was backwards.

    So the line aims to sit CONTOUR_CONTRAST away from whatever it crosses,
    going darker where there is room below and lighter where there is not, and
    the coverage from contour_style then weights it exactly as the viewer's
    line-opacity weights the vector version of the same line. The flip is kept
    one-sided rather than switched partway along a line, because a line that
    changed from dark to light would read as two different lines.

    Contrast is luminance, not hue, which is also what makes it legible to a
    reader who cannot separate the colours.
    """
    ss = CONTOUR_SUPERSAMPLE
    mask = Image.new("L", (TILE * ss, TILE * ss), 0)
    draw = ImageDraw.Draw(mask)
    for coverage, pen, points in lines:
        draw.line(points.ravel().tolist(), width=pen, joint="curve", fill=coverage)

    weight = np.asarray(mask.resize((TILE, TILE), Image.BOX), dtype=np.uint16)
    if sea is not None:
        weight = weight * sea
    if not weight.any():
        return base

    wide = base.astype(np.uint16)
    # Rec. 709 luminance in integers: 54/183/18 sum to 255.
    lum = (wide[..., 0] * 54 + wide[..., 1] * 183 + wide[..., 2] * 18) >> 8

    # One-sided, and that is deliberate. Any smooth flip between a darker and a
    # lighter line passes through a point where the two cancel and the contrast
    # is zero, so there is no gradual version of this. Instead the fixed tone is
    # kept wherever it already separates well -- which is the pale shelf it was
    # chosen against, and where nobody complained -- and only water too close to
    # it gets a lighter line. The switch then happens at the shelf edge, between
    # two depths that already look different.
    short = np.abs(lum.astype(np.int16) - CONTOUR_RGB_LUMINANCE) < CONTOUR_CONTRAST
    want = np.where(short, np.minimum(lum + CONTOUR_CONTRAST, 249),
                    CONTOUR_RGB_LUMINANCE).astype(np.float32)
    # Scaling the background to the wanted luminance keeps the water's own hue,
    # so the line still reads as part of the map rather than ink over it. Where
    # the fixed tone is kept, scale that instead.
    source = np.where(short[..., None], wide, np.array(CONTOUR_RGB, np.uint16))
    source_lum = np.where(short, lum, CONTOUR_RGB_LUMINANCE)
    tone = np.clip(source * (want / np.maximum(source_lum, 1))[..., None],
                   0, 255).astype(np.uint16)

    weight = weight[..., None]
    return ((wide * (255 - weight) + tone * weight) // 255).astype(np.uint8)


def _paint_water(base: np.ndarray, water: Water | None, z: int, x: int, y: int,
                 on_land: np.ndarray | None = None,
                 parts: Composite | None = None) -> np.ndarray:
    """Coastal water, then rivers and lakes over it, then the lake beds.

    Order matters: a HydroLAKES lake inside the coastal-water area keeps its
    own ramp, and a river running into a lagoon disappears into it. The beds
    go last, over the flat lake tone, inset from the shore as the archive
    draws them -- the same stack the viewer builds from its layers.
    """
    if water is None:
        return base
    # An all-sea tile has nothing to paint, and the read is the most expensive
    # thing in the tile; most of the world is that case.
    if on_land is None or on_land.any():
        coverage = water.coastal(z, x, y)
        depth = water.depth(z, x, y) if coverage is not None and coverage.any() else None
        base = _draw_coastal(base, coverage, on_land, z, depth)
    lakes, rivers = water.at(z, x, y)
    base = _draw_water(base, lakes, rivers, z, x, y)
    if parts is not None and lakes:
        base = _draw_lake_relief(base, parts, z, x, y)
    return base


def _draw_lake_relief(base: np.ndarray, parts: Composite, z: int, x: int, y: int) -> np.ndarray:
    """The lake-bed archive over the flat lake tone, where it has a tile.

    The tree had no beds at all: the viewer draws lake_relief as a layer, and
    nothing here read it, so Baikal and Tanganyika were one flat tone from z4
    up while the vector map showed their floors. Read like the other raster
    archives, with the nearest ancestor stretched past its deepest zoom.
    """
    image, _ = _from_ancestor(parts, list(LAKE_RELIEF_ARCHIVES), z, x, y)
    if image is None:
        return base
    bed = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    alpha = bed[..., 3:4]
    if not alpha.any():
        return base
    return ((base.astype(np.uint16) * (255 - alpha) + bed[..., :3] * alpha)
            // 255).astype(np.uint8)


def _is_opaque(image: Image.Image) -> bool:
    """True when nothing shows through -- so nothing needs compositing under it."""
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    return alpha is None or alpha.getextrema()[0] == 255


def _grow(alpha: np.ndarray) -> np.ndarray:
    """Alpha dilated by one pixel, in four shifted comparisons."""
    out = alpha.copy()
    out[:-1, :] = np.maximum(out[:-1, :], alpha[1:, :])
    out[1:, :] = np.maximum(out[1:, :], alpha[:-1, :])
    out[:, :-1] = np.maximum(out[:, :-1], alpha[:, 1:])
    out[:, 1:] = np.maximum(out[:, 1:], alpha[:, :-1])
    return out


# ---------------------------------------------------------------------------
# Inland water
# ---------------------------------------------------------------------------
#
# The flat tree's first run had no lakes in it at all: the vector map draws
# them from lakes.pmtiles over the relief, and there is no vector layer here.
# Every lake in the world came out as land, which is a release blocker rather
# than a blemish -- the Great Lakes, the Caspian, Baikal, and every fjordside
# vatn in Norway.
#
# They are rasterised from the same FlatGeobuf bands and the same ramp the
# viewer uses, so the two maps agree. The bands matter: tiling every pond from
# z2 would bury the few hundred lakes legible there, so each zoom takes lakes
# above an area threshold, exactly as LAKE_BANDS sets it.

# Coastal water -- lagoons, estuaries, tidal flats -- drawn as inland water
# rather than as sea. Putting it in the sea mask was the first attempt and it
# does not work: the bathymetry ramp is near-white in its first two metres, so
# the Venice lagoon came out the same tone as the pale land beside it and
# could not be told apart. EMODnet draw the same water as a flat tone and keep
# the bathymetric ramp for the open sea, which is why theirs reads at a glance.
#
# The tone is a colour of its own, 20 luminance levels under the sea ramp's
# surface stop. It was the surface stop itself for a while, on the reasoning
# that a lagoon and the shelf it opens onto are the same water, and that is
# true of the water and wrong about the map: at the surface stop the Camargue's
# lagoons, Venice's and Ria Formosa's dissolved into the pale shelf and the
# pale land both, while EMODnet draws every one of them as a distinct body at
# luminance 182-196. Measured against their tiles, the sea and the land here
# already matched theirs to the pixel; the lagoon tone was the difference.
# This one sits at 196, judged on those three sites and then the Danube delta,
# the Oslofjord and the IJsselmeer, and it is also the lake ramp's surface
# stop (color-ramps/lakes.txt), so a lake and the lagoon beside it are one
# kind of water. The earlier worry -- that a lagoon darker than the sea makes
# the sea look washed out -- was judged on the Danube delta with this tone
# and the H sea ramp together and did not hold.
#
# A threshold with a two-point band for the edge, rather than the fade from 50
# to 80 that drew this first. The fade was meant for channels -- a 30 m cell
# half channel and half mudflat is half water -- but it also painted everything
# seasonally wet at half strength: the rice fields of the Camargue and the
# irrigated Po plain sit at 40 to 70% occurrence and came out under a pale veil
# that read as a grey-white haze over the whole delta. EMODnet draws a lagoon
# as one body with a crisp edge on flat land; measured against their tiles the
# colours here already matched and the edge did not. Cut at 65 the fields drop
# out, Venice's channels and its northern marsh stay (at 80 that marsh broke
# into islands), and the edge is sharp enough to carry an outline. The band
# keeps the edge from stepping cell to cell on the warped occurrence.
# Against land at about 215 and the sea's surface at 216 it reads as neither,
# and it carries the difference on luminance and on the blue-yellow axis, the
# two a red-green reader has.
COASTAL_WATER_RGB = (165, 203, 232)          # also lakes.txt at 0 m
COASTAL_WATER_THRESHOLD = 65.0
COASTAL_WATER_EDGE = 2.0
# The lagoon's outline, from this zoom up: the lakes' pen and tone
# (LAKE_OUTLINE_RGBA), which is what makes a lagoon and the étang beside it read
# as the same kind of thing. Below it a one-pixel line round every pond is noise.
COASTAL_OUTLINE_MIN_ZOOM = 9
# Where the bathymetry says a body of water is deep, it is sea and takes the
# sea ramp's colour at its depth, flat; the lagoon tone is for the shallow
# water the coastline wrongly calls land. The Amazon showed why. The OSM
# coastline runs through its mouth, so the channels inland of it -- 10 to
# 30 m deep in GEBCO, sounded and contoured -- were painted as a lagoon, 20
# luminance levels darker than the same water a tile further out, and darker
# than the whole estuary at z5 where the coarse coastline still calls it sea.
# And at z8 to z10 the global sea archive's alpha edge, stretched from z8,
# let the light sea show through the lagoon tone as a blocky glow. With both
# sides on the ramp there is no seam to see.
#
# Judged on the body, not the pixel. GEBCO has soundings inside the lagoons
# too -- the Venice lagoon is -1 to -2 m with -15 m shipping channels, the
# Camargue's étangs -2, the Danube limans -2 to -4 -- so a per-pixel cut at
# any depth paints patches of sea into a lagoon and patches of lagoon into
# the Amazon, where the margins shoal to -1. Measured over the land-side water
# of all those sites, a mean depth over a 5 km window separates them: the
# lagoons sit at -2, the Amazon at -12. The colour blends from the lagoon
# tone at the first depth to the ramp at the second, so no edge is ever hard.
COASTAL_SEA_RADIUS_M = 2500.0
COASTAL_SEA_BLEND = (-3.0, -7.0)
# Above this the occurrence raster is read a strip of tiles at a time rather
# than one tile at a time; below it, per tile. A strip spans eight tiles of
# latitude, and the taller that is in ground terms the more a single warp over
# it drifts from eight separate ones: measured against the per-tile read the
# coverage agrees exactly at z12, by 0.005 at z11, and by 0.14 at z7. z11 and
# z12 are 94% of the tiles in the tree and carry all of the cost, so the cheap
# read is taken exactly where it pays and nowhere else.
COVER_STRIP_MIN_ZOOM = 11

LAKE_RELIEF_ARCHIVES = ("lake_relief",)
# Alpha 119: the panel's 0.50 against the layer's 0.80 default, which 190 was
# matched to.
LAKE_OUTLINE_RGBA = (122, 167, 199, 119)
# Rivers are stretches of the MERIT network (pipeline/rivers.py), chosen per
# zoom by Strahler order or mean discharge with the same rule the vector
# archive is built on, so the two maps show the same rivers. They were Natural
# Earth's centrelines -- a few hundred of the world's largest -- which at z10
# drew the Rhône through the Camargue and none of the drainage that gives the
# delta its shape. Drawn under the lakes so a river meeting a lake disappears
# into it. The pen, in tile pixels, grows with the stretch: a great river is a
# line, a tributary a hairline. The discharge steps are half HydroRIVERS',
# like the selection's. A dry channel -- under rivers.DRY_DIS -- is a dashed
# hairline: told from a river by its pattern, not its colour.
RIVER_RGBA = (118, 168, 204, 190)
RIVER_PENS: tuple[tuple[int, float, float], ...] = (
    (7, 150.0, 2.0),      # order 7 up, or 150 m³/s: the Rhône, the Po
    (5, 50.0, 1.4),
    (0, 0.0, 0.9),
)
# Dash and gap of a dry channel, tile pixels.
DRY_DASH = (4.0, 3.0)
WATER_SUPERSAMPLE = 2


def river_pen(order: int, discharge: float) -> float:
    for least_order, least_discharge, pen in RIVER_PENS:
        if order >= least_order or (least_discharge and discharge >= least_discharge):
            return pen
    return RIVER_PENS[-1][2]


def _elevation_source(work: Path):
    """GEBCO, for deciding whether observed water is coastal.

    The occurrence raster sees every river and reservoir on earth, and the
    archive painted all of them at the sea's surface tone. Inland that is the
    brightest thing on the map -- 11 luminance levels over the land beside it
    once palette D moved the tone up -- so an arm of the Alqueva reservoir read
    as a glow rather than as water. Coastal water is for the shore, where the
    coastline wrongly claims land; inland water belongs to the lakes.
    """
    from basemap.config import GEBCO_RELEASE
    from basemap.processing.raster_tiles import Source

    tif = work.parent / f"GEBCO_{GEBCO_RELEASE}.tif"
    if not tif.is_file():
        raise SystemExit(f"missing {tif}; the coastal-water gate needs GEBCO")
    return Source(str(tif))


def _occurrence_source(work: Path):
    """The surface-water mosaic, as the bathymetry step builds it."""
    from basemap.pipeline import surface_water as sw

    base = work / "water" / "gsw" / "gsw_occurrence.vrt"
    if not base.is_file():
        raise SystemExit(f"missing {base}; run the surface-water step first")
    from basemap.processing.raster_tiles import Source

    pyramid = tuple(
        (sw.NATIVE_DEG * factor, str(work / "water" / "gsw" / f"gsw_occurrence_ov{i}.vrt"))
        for i, factor in enumerate(sw.OVERVIEW_LEVELS)
        if (work / "water" / "gsw" / f"gsw_occurrence_ov{i}.vrt").is_file()
    )
    return Source(str(base), valid=(0.0, 100.0), pyramid=pyramid)


def _lake_band_file(work: Path, z: int) -> Path | None:
    from basemap.config import LAKE_BANDS

    for min_area, (lo, hi) in LAKE_BANDS:
        if lo <= z <= hi:
            name = "all" if min_area <= 0 else f"{min_area:g}"
            return work / "lakes" / f"lakes_{name}.fgb"
    # Past the last band -- the detail zooms -- every lake stays, as it does at
    # the band's floor: a lake that is drawn at z12 cannot vanish at z13.
    min_area, (_, deepest) = LAKE_BANDS[-1]
    if z > deepest:
        name = "all" if min_area <= 0 else f"{min_area:g}"
        return work / "lakes" / f"lakes_{name}.fgb"
    return None


class Water:
    """Lakes and rivers for a tile, cached over a block of tiles at a time.

    A bbox query into a 1.9 GB FlatGeobuf is cheap but not free, and at z12
    there are sixteen million tiles. The features are read once per block of
    2^GROUP tiles a side and reused across it, the same trick the contours use
    and the same one the main renderer uses for its metatiles.
    """

    GROUP = 3

    __slots__ = ("_work", "_rivers_path", "_key", "_lakes", "_rivers", "_occurrence",
                 "_cover_key", "_cover", "_want_lakes", "_elev", "_gate_key", "_gate",
                 "_elev_key", "_elev_strip")

    @staticmethod
    def rivers_path(work: Path) -> Path:
        from basemap.pipeline.rivers import rivers_fgb

        return rivers_fgb(work)

    def __init__(self, work: Path, *, lakes: bool = True):
        # Loudly, because the quiet version of this shipped: the first run of
        # the tree had no lakes in it and said nothing, and a missing lake is
        # indistinguishable from a lake that is genuinely not in this tile.
        # Anything that would silently draw no water is an error here instead.
        #
        # lakes=False is for a caller that wants only the observed coastal
        # water -- the coastal_water archive -- and would otherwise be made to
        # stage HydroLAKES to read a raster it does not use.
        self._want_lakes = lakes
        if lakes and not (work / "lakes").is_dir():
            raise SystemExit(f"no lakes in {work}; run the lakes step first")
        self._work = work
        # The rivers are required for the same reason the lakes are: a tree
        # without them looks finished and is not the one that was judged.
        river = self.rivers_path(work)
        self._rivers_path = str(river) if river.is_file() else None
        if lakes and self._rivers_path is None:
            raise SystemExit(f"missing {river}; run the rivers step first")
        self._key: tuple[int, int, int] | None = None
        self._lakes: list[tuple] = []
        self._rivers: list = []
        self._occurrence = _occurrence_source(work)
        self._cover_key: tuple[int, int, int] | None = None
        self._cover: np.ndarray | None = None
        self._elev = _elevation_source(work)
        self._gate_key: tuple[int, int, int] | None = None
        self._gate: np.ndarray | None = None
        self._elev_key: tuple | None = None
        self._elev_strip: np.ndarray | None = None

    def _load(self, z: int, gx: int, gy: int) -> None:
        from basemap.processing.raster_tiles import MERCATOR

        side = 1 << self.GROUP
        w, s, _, _ = MERCATOR.tile_lonlat_bounds(z, gx * side, (gy + 1) * side - 1)
        _, _, e, n = MERCATOR.tile_lonlat_bounds(z, (gx + 1) * side - 1, gy * side)
        pad = (e - w) / TILE                      # a tile pixel, so edges meet
        box = (w - pad, s - pad, e + pad, n + pad)

        band = _lake_band_file(self._work, z) if self._want_lakes else None
        if band is None:
            # No band covers z0-z1, and that is correct rather than missing:
            # LAKE_BANDS starts at z2 because at world scale the Caspian is two
            # pixels across. An absent band means no lakes at this zoom; only
            # an absent *file* for a band that exists is an error.
            self._lakes = []
            self._rivers = []
            return
        if not band.is_file():
            raise SystemExit(f"missing {band}; run the lakes step first")
        # Two attributes are needed -- the mean depth, and the surface
        # elevation that decides whether a lake is drawn on the lake ramp or
        # the sea's -- and polygons_in returns one, so they are read together.
        self._lakes = _lakes_in(str(band), box)
        self._rivers = []
        if self._rivers_path:
            self._rivers = _rivers_in(self._rivers_path, box, z)

    def at(self, z: int, x: int, y: int) -> tuple[list[tuple], list]:
        key = (z, x >> self.GROUP, y >> self.GROUP)
        if key != self._key:
            self._key = key
            self._load(*key)
        return self._lakes, self._rivers

    def coastal(self, z: int, x: int, y: int) -> np.ndarray | None:
        """Per-pixel coastal-water coverage, 0..1, or None below the floor.

        Read a strip at a time. Per tile this cost 40 ms -- measured against
        0.01 ms for the contours and nothing at all for the lakes, both of
        which already cache per parent -- and at twenty-two million tiles that
        one read was seventeen hours of the render on its own. The window is
        small; the cost is opening and warping the mosaic, so it is done once
        and sliced.
        
        A *strip*, one tile wide and 2^GROUP tall, because that is the shape
        the renderer walks: _render_batch takes one column and runs down its
        y. A square block of the same GROUP reads eight times the data to
        serve the same column and measured slower than no cache at all.
        """
        from basemap.pipeline.surface_water import WATER_MIN_ZOOM

        if self._occurrence is None or z < WATER_MIN_ZOOM:
            return None
        # One exit, through the gate. It first went on the two strip-cached
        # paths and not on this one, which left every zoom below
        # COVER_STRIP_MIN_ZOOM ungated -- the zooms a reader actually looks at
        # a reservoir from. The archive verified clean at z12 and still glowed
        # at z10.
        if z < COVER_STRIP_MIN_ZOOM:
            return self._gated(self._read_strip(z, x, y, rows=1), z, x, y, rows=1)
        key = (z, x, y >> self.GROUP)
        if key != self._cover_key:
            self._cover_key, self._cover = key, self._read_strip(z, x, key[2])
        if self._cover is None:                       # rows disagreed; read alone
            return self._gated(self._read_strip(z, x, y, rows=1), z, x, y, rows=1)
        oy = (y - (key[2] << self.GROUP)) * TILE
        return self._gated(self._cover, z, x, key[2])[oy:oy + TILE]

    def _gated(self, cover, z: int, x: int, gy: int, rows: int | None = None):
        """Coverage with everything above the coastal line taken out.

        The same elevation the lakes are split on: at or below
        COASTAL_LAKE_ELEV a body reads as the sea it joins, above it as inland
        water. Observed water inland is already drawn by the lakes, with its
        own depth ramp; painting the sea's flat surface tone over it as well is
        what made the reservoir arms glow.

        Read as a strip on the same key as the coverage, so this costs one warp
        per strip rather than one per tile, and only where there is water to
        gate at all -- most strips return early.
        """
        from basemap.config import COASTAL_LAKE_ELEV

        if cover is None or not cover.any():
            return cover
        key = (z, x, gy, rows or 0)
        if key != self._gate_key:
            elev = self._read_strip(z, x, gy, rows=rows, source=self._elev)
            self._gate_key = key
            self._gate = None if elev is None else (
                np.nan_to_num(elev, nan=0.0) <= COASTAL_LAKE_ELEV)
        if self._gate is None or self._gate.shape != cover.shape:
            return cover
        return cover * self._gate

    @staticmethod
    def depth_radius_px(z: int, x: int, y: int) -> int:
        """COASTAL_SEA_RADIUS_M in pixels of tile row *y* at *z*."""
        return max(1, round(COASTAL_SEA_RADIUS_M / MERCATOR_PIXEL_M(z, x, y)))

    def depth(self, z: int, x: int, y: int) -> np.ndarray | None:
        """Mean GEBCO elevation over the water within COASTAL_SEA_RADIUS_M of
        each pixel, on the tile's grid, or None below the water floor.

        Read a strip at a time like the coverage, padded by the radius so the
        window never sees a strip edge, and cached beside it. The mean is a
        box over the observed-water pixels, through integral images.
        """
        from basemap.pipeline.surface_water import WATER_MIN_ZOOM

        if self._occurrence is None or z < WATER_MIN_ZOOM:
            return None
        if z < COVER_STRIP_MIN_ZOOM:
            key, gy, rows = (z, x, y, 1), y, 1
        else:
            key, gy, rows = (z, x, y >> self.GROUP, 0), y >> self.GROUP, None
        if key != self._elev_key:
            # The radius in pixels comes from a tile row, never from the strip
            # index: gy names a strip of 2^GROUP rows, and read as a row it put
            # the pixel size near the pole and widened the window several times.
            top = gy if rows == 1 else gy << self.GROUP
            pad = self.depth_radius_px(z, x, top)
            cover = self._read_strip(z, x, gy, rows=rows, pad=pad)
            elev = self._read_strip(z, x, gy, rows=rows, pad=pad, source=self._elev)
            strip = None
            if cover is not None and elev is not None:
                wet = cover >= 0.5
                mean = _box_mean(np.nan_to_num(elev, nan=0.0), wet, pad)
                strip = mean[pad:-pad, pad:-pad]
            self._elev_key, self._elev_strip = key, strip
        if self._elev_strip is None:
            return None
        if rows == 1:
            return self._elev_strip
        oy = (y - (gy << self.GROUP)) * TILE
        return self._elev_strip[oy:oy + TILE]

    def _read_strip(self, z: int, x: int, gy: int, rows: int | None = None,
                    source=None, pad: int = 0):
        """Coverage down one column of tiles, on the tile grid, in one read.

        Returns None when the rows of the strip would not all have chosen the
        same stand-in from the pyramid on their own. The ground resolution
        changes down a strip, and at low zooms a strip spans enough latitude
        that its top and bottom fall either side of a pyramid boundary -- read
        together, some rows then come from a different source than they would
        have alone, and at a coastline that flips a pixel between land and
        water outright. Measured at z7 over Iberia as a full 1.0 difference.
        Those strips are read a tile at a time instead, which is what the
        zooms with most of the tiles in them never need.
        """
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        from rasterio.warp import reproject

        from basemap.processing.raster_tiles import MERCATOR, _dataset

        if rows is None:
            top = gy << self.GROUP
            rows = min(1 << self.GROUP, (1 << z) - top)
        else:
            top = gy                                   # gy is a tile row here
        src = source or self._occurrence
        levels = {
            src.at(MERCATOR.pixel_ground_m(z, x, top + r)[1] / 111320.0)
            for r in range(rows)
        }
        if len(levels) > 1:
            return None
        w, s, _, _ = MERCATOR.tile_bounds(z, x, top + rows - 1)
        _, _, e, n = MERCATOR.tile_bounds(z, x, top)
        if pad:
            # Past the strip on every side, so a window centred on an edge
            # pixel sees its whole neighbourhood; the caller crops it back.
            step = (e - w) / TILE
            w, s, e, n = w - pad * step, s - pad * step, e + pad * step, n + pad * step
        width, height = TILE + 2 * pad, TILE * rows + 2 * pad
        out = np.full((height, width), np.nan, dtype=np.float32)
        pixel_deg = MERCATOR.pixel_ground_m(z, x, top)[1] / 111320.0
        ds = _dataset(levels.pop(), pixel_deg)
        reproject(
            source=rasterio.band(ds, 1), destination=out,
            src_crs=ds.crs, src_transform=ds.transform, src_nodata=ds.nodata,
            dst_transform=from_bounds(w, s, e, n, width, height), dst_crs=MERCATOR.crs,
            dst_nodata=np.nan, resampling=Resampling.bilinear,
            # Exact transformer. The default approximates over the window, and
            # over a strip eight tiles tall at z7 that drifted the coverage by
            # up to 0.14 against the per-tile read.
            ERROR_THRESHOLD=0.0,
        )
        if source is not None:                        # elevation, not coverage
            return out
        lo, hi = self._occurrence.valid
        out[(out < lo) | (out > hi)] = 0.0
        return coastal_coverage(out)


_WATER_CACHE: dict = {}


def MERCATOR_PIXEL_M(z: int, x: int, y: int) -> float:
    from basemap.processing.raster_tiles import MERCATOR

    return MERCATOR.pixel_ground_m(z, x, y)[0]


def _box_mean(values: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    """Mean of *values* over *mask* within a square of *radius*, per pixel;
    NaN where the square holds no masked pixel. Integral images, so the cost
    does not grow with the radius."""
    v = np.where(mask, values, 0.0).astype(np.float64)
    m = mask.astype(np.float64)

    def integral(a):
        return np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)

    sv, sm = integral(v), integral(m)
    rows, cols = v.shape
    ys, xs = np.arange(rows), np.arange(cols)
    y0 = np.clip(ys - radius, 0, rows)[:, None]
    y1 = np.clip(ys + radius + 1, 0, rows)[:, None]
    x0 = np.clip(xs - radius, 0, cols)[None, :]
    x1 = np.clip(xs + radius + 1, 0, cols)[None, :]
    total = sv[y1, x1] - sv[y0, x1] - sv[y1, x0] + sv[y0, x0]
    count = sm[y1, x1] - sm[y0, x1] - sm[y1, x0] + sm[y0, x0]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan).astype(np.float32)


def _lakes_in(path: str, lonlat) -> list[tuple]:
    """(geometry, mean depth, surface elevation, area) for lakes in a box."""
    import fiona
    import shapely
    from shapely.geometry import shape

    from basemap.processing.raster_tiles import MERCATOR

    src = _WATER_CACHE.get(path)
    if src is None:
        src = _WATER_CACHE[path] = fiona.open(path)
    corrected = _WATER_CACHE.setdefault("__depths__", lake_depth_overrides())
    out = []
    for feature in src.filter(bbox=lonlat):
        props = feature["properties"]
        geometry = shape(feature["geometry"])
        if geometry.is_empty:
            continue
        if not geometry.is_valid:
            geometry = shapely.make_valid(geometry)
        # HydroLAKES models the mean depth rather than measuring it, and is out
        # by an order of magnitude on some lakes: see lake-depths.json.
        depth = corrected.get(props.get("name"), props.get("depth_avg") or 0.0)
        out.append((shapely.transform(geometry, MERCATOR.to_projected),
                    float(depth),
                    float(props.get("elev_m") or 0.0),
                    float(props.get("area_km2") or 0.0)))
    return out


def _rivers_in(path: str, lonlat, z: int) -> list[tuple]:
    """(line in Web Mercator, order, discharge) for the reaches drawn at *z*.

    A bbox query into the staged rivers FlatGeobuf, which carries a spatial
    index, filtered by the one rule the vector archive is built with.
    """
    import fiona
    import shapely
    from shapely.geometry import shape

    from basemap.pipeline.rivers import selection
    from basemap.processing.raster_tiles import MERCATOR

    rule = selection(z)
    if rule is None:
        return []
    least_order, least_discharge = rule
    src = _WATER_CACHE.get(path)
    if src is None:
        src = _WATER_CACHE[path] = fiona.open(path)
    out = []
    for feature in src.filter(bbox=lonlat):
        props = feature["properties"]
        order = int(props.get("order") or 0)
        discharge = float(props.get("dis") or 0.0)
        if order < least_order and discharge < least_discharge:
            continue
        geometry = shape(feature["geometry"])
        if geometry.is_empty:
            continue
        out.append((shapely.transform(geometry, MERCATOR.to_projected), order, discharge))
    return out


def _water_colour(depth: float, elev: float = 0.0, area: float = 0.0) -> tuple[int, int, int]:
    """A lake's tone: the lake ramp, at its mean depth. Every lake.

    Lakes near sea level used to take the sea's ramp instead, on the reasoning
    that a liman reads as the shelf it opens onto. The reasoning is sound and
    the test for it was not: neither surface elevation nor area measures whether
    a lake meets the sea, and the two ramps are 44 luminance levels apart at the
    same depth, so every misclassification showed as a lake of a visibly
    different kind beside its neighbour.

    It misfired three times on real maps. Ladoga, which HydroLAKES records at
    -10 m, went pale beside Onega at 13 m. Adding a depth test then threw out
    the Etang de Berre, a Mediterranean lagoon HydroLAKES models deeper than
    Onega. Swapping depth for area threw in Sarygamysh, a closed lake in the
    Karakum 400 km from any sea, at 2 m and 3,772 km2.

    One ramp for all of them, so no two lakes can differ except by depth. Water
    that is genuinely continuous with the sea and that OSM's coastline calls
    land -- the Venice lagoon, the Wadden, the limans -- is painted by the
    observed-water raster instead, which is a separate layer and keeps the sea's
    own surface tone. *elev* and *area* are kept so callers need not change.
    """
    rgb = _LAKE_RAMP.colour(np.array([[-abs(depth)]]))[0, 0]
    return int(rgb[0]), int(rgb[1]), int(rgb[2])


def _ramps():
    from basemap.config import RAMP_DIR
    from basemap.processing.raster_tiles import Ramp

    return Ramp.parse((RAMP_DIR / "lakes.txt").read_text())


_LAKE_RAMP = _ramps()


def coastal_coverage(occurrence: np.ndarray) -> np.ndarray:
    """Occurrence, 0-100 with NaN where never observed, to coverage 0..1."""
    low = COASTAL_WATER_THRESHOLD - COASTAL_WATER_EDGE
    return np.clip((np.nan_to_num(occurrence, nan=0.0) - low) / (2.0 * COASTAL_WATER_EDGE),
                   0.0, 1.0)


def coastal_rgba(coverage: np.ndarray, outline: bool,
                 depth: np.ndarray | None = None) -> np.ndarray:
    """The coastal-water layer as RGBA: the flat tone at *coverage*, and with
    *outline* the lakes' line along its edge.

    One function for the two products: the tree blends this over its land, and
    the coastal_water archive writes it as it is. The edge is where covered
    water meets uncovered -- so where a lagoon opens into the sea nothing is
    drawn, because the water simply continues, and the compositor keeps the
    sea side off with its own mask.

    With *depth* -- the mean bathymetry over the water around each pixel, see
    Water.depth -- the colour blends from the lagoon tone at the first depth
    in COASTAL_SEA_BLEND to the sea ramp's colour at the second.
    """
    rgba = np.zeros((*coverage.shape, 4), dtype=np.uint8)
    rgba[..., :3] = COASTAL_WATER_RGB
    if depth is not None:
        d = np.nan_to_num(np.asarray(depth, dtype=np.float32), nan=0.0)
        shallow, deep = COASTAL_SEA_BLEND
        t = np.clip((shallow - d) / (shallow - deep), 0.0, 1.0)
        if (t > 0).any():
            sea = _sea_ramp().colour(np.clip(d, -12000, 0))
            mixed = (np.array(COASTAL_WATER_RGB, np.float32) * (1 - t[..., None])
                     + sea * t[..., None])
            rgba[..., :3] = mixed.round().astype(np.uint8)
    rgba[..., 3] = np.clip(coverage * 255.0, 0, 255).astype(np.uint8)
    if outline:
        wet = coverage >= 0.5
        inner = wet.copy()
        inner[1:] &= wet[:-1]
        inner[:-1] &= wet[1:]
        inner[:, 1:] &= wet[:, :-1]
        inner[:, :-1] &= wet[:, 1:]
        edge = wet & ~inner
        if edge.any():
            r, g, b, a = LAKE_OUTLINE_RGBA
            tone = (rgba[edge, :3].astype(np.float32) * (1 - a / 255.0)
                    + np.array([r, g, b], np.float32) * (a / 255.0))
            rgba[edge, :3] = tone.round().astype(np.uint8)
            rgba[edge, 3] = 255
    return rgba


_SEA_RAMP = None


def _sea_ramp():
    """The published sea ramp, for coastal water the bathymetry calls sea."""
    global _SEA_RAMP
    if _SEA_RAMP is None:
        from basemap.config import water_ramp_file
        from basemap.processing.raster_tiles import Ramp

        _SEA_RAMP = Ramp.parse(water_ramp_file().read_text())
    return _SEA_RAMP


def _draw_coastal(base: np.ndarray, coverage: np.ndarray | None,
                  on_land: np.ndarray | None, z: int,
                  depth: np.ndarray | None = None) -> np.ndarray:
    """The coastal-water layer over the tile, on the land side only.

    *on_land* is what keeps this off the sea. The occurrence raster marks the
    coastal ocean as permanent water too, so without the mask this paints flat
    blue over the shelf and erases the bathymetry with it -- which is exactly
    what the first attempt at this did.
    """
    if coverage is None:
        return base
    layer = coastal_rgba(coverage, outline=z >= COASTAL_OUTLINE_MIN_ZOOM, depth=depth)
    a = layer[..., 3].astype(np.float32) / 255.0
    if on_land is not None:
        a = a * on_land
    if not a.any():
        return base
    a = a[..., None]
    return (base.astype(np.float32) * (1.0 - a)
            + layer[..., :3].astype(np.float32) * a).round().astype(np.uint8)


def _draw_water(base: np.ndarray, lakes, rivers, z: int, x: int, y: int) -> np.ndarray:
    """Paint rivers and lakes onto the land, anti-aliased.

    Drawn at WATER_SUPERSAMPLE and averaged down. A lake is one tone -- the
    mean depth HydroLAKES models for it, not bathymetry -- so the fill is flat
    and only its edge needs the smoothing. Rivers go under the lakes, so a
    river meeting a lake runs into it rather than across it.
    """
    if not lakes and not rivers:
        return base

    ss = WATER_SUPERSAMPLE
    size = TILE * ss
    west, south, east, north = _tile_metre_bounds(z, x, y)
    span_x = (east - west) or 1.0
    span_y = (north - south) or 1.0

    def to_px(coords):
        out = []
        for mx, my in coords:
            out.append(((mx - west) / span_x * size, (north - my) / span_y * size))
        return out

    ink = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(ink)

    from basemap.pipeline.rivers import DRY_DIS

    for line, order, discharge in rivers:
        width = max(1, int(round(river_pen(order, discharge) * ss)))
        for part in _parts(line):
            points = to_px(part)
            if len(points) < 2:
                continue
            if discharge < DRY_DIS:
                for dash in _dashes(points, DRY_DASH[0] * ss, DRY_DASH[1] * ss):
                    pen.line([c for p in dash for c in p], fill=RIVER_RGBA, width=width)
            else:
                pen.line([c for p in points for c in p], fill=RIVER_RGBA,
                         width=width, joint="curve")

    for geometry, depth, elev, area in lakes:
        fill = (*_water_colour(depth, elev, area), 255)
        for ring, holes in _rings(geometry):
            points = to_px(ring)
            if len(points) < 3:
                continue
            pen.polygon([c for p in points for c in p], fill=fill,
                        outline=LAKE_OUTLINE_RGBA, width=ss)
            for hole in holes:
                cut = to_px(hole)
                if len(cut) >= 3:
                    pen.polygon([c for p in cut for c in p], fill=(0, 0, 0, 0))

    small = ink.resize((TILE, TILE), Image.BOX)
    rgba = np.asarray(small, dtype=np.uint16)
    alpha = rgba[..., 3:4]
    if not alpha.any():
        return base
    return ((base.astype(np.uint16) * (255 - alpha) + rgba[..., :3] * alpha)
            // 255).astype(np.uint8)


def _dashes(points, on: float, off: float):
    """The dashes of a polyline, as point lists, *on* drawn and *off* skipped.

    The pattern starts at the line's first point, wherever the tile is: every
    tile is handed the whole line, so the dashes meet across tile edges.
    """
    import math

    dash: list = []
    drawing, left = True, on
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        done = 0.0
        while seg - done > 1e-9:
            step = min(left, seg - done)
            a, b = done / seg, (done + step) / seg
            start = (x0 + (x1 - x0) * a, y0 + (y1 - y0) * a)
            end = (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)
            if drawing:
                if not dash:
                    dash.append(start)
                dash.append(end)
            done += step
            left -= step
            if left <= 1e-9:
                if drawing and len(dash) > 1:
                    yield dash
                dash = []
                drawing = not drawing
                left = on if drawing else off
    if drawing and len(dash) > 1:
        yield dash


def _parts(geometry):
    """Coordinate lists of a line or multi-line geometry."""
    if geometry.geom_type == "LineString":
        return [list(geometry.coords)]
    if hasattr(geometry, "geoms"):
        out = []
        for part in geometry.geoms:
            out += _parts(part)
        return out
    return []


def _rings(geometry):
    """(exterior, interiors) of every polygon in a geometry."""
    if geometry.geom_type == "Polygon":
        return [(list(geometry.exterior.coords),
                 [list(r.coords) for r in geometry.interiors])]
    if hasattr(geometry, "geoms"):
        out = []
        for part in geometry.geoms:
            out += _rings(part)
        return out
    return []


def _tile_metre_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    from basemap.processing.raster_tiles import MERCATOR

    return MERCATOR.tile_bounds(z, x, y)


def _require_contours(tile_dir: Path) -> Path:
    """The contour archive, or a refusal to render a tree without contours.

    Loudly, for the same reason the lakes are: the first published tree had no
    contours in any of its twenty-two million tiles and nothing said so. The
    archive is built by the contours step and has to be staged with the other
    inputs; absent, this used to evaluate to None and draw nothing, which is
    indistinguishable in the output from a tile that genuinely has no contour
    crossing it.
    """
    archive = tile_dir / f"{CONTOUR_ARCHIVE}.pmtiles"
    if not archive.is_file():
        raise SystemExit(f"missing {archive}; run the contours step, or stage "
                         f"it with the build inputs")
    return archive


def detail_archive(tile_dir: Path) -> Archive:
    path = tile_dir / f"{DETAIL_ARCHIVE}.pmtiles"
    if not path.is_file():
        raise SystemExit(f"missing {path}; the detail zooms follow the HR archive's tiles")
    return Archive(path)


def detail_columns(archive: Archive, z: int, shard: Shard = Shard()) -> dict[int, list[int]]:
    """The rows to render per column at a detail zoom: where *archive* has tiles.

    Keyed like the full zooms -- a column is the unit of work and of resume --
    but a column here is only the rows the archive holds, not the whole side.
    """
    columns: dict[int, list[int]] = {}
    for x, y in archive.tiles_at(z):
        if x % shard.count == shard.index:
            columns.setdefault(x, []).append(y)
    for rows in columns.values():
        rows.sort()
    return columns


def _render_batch(args) -> list[tuple[int, int, int, bytes]]:
    # (tile_dir, work_dir, z, columns, y0, y1) renders whole columns; a seventh
    # item names the rows explicitly, which is how the detail zooms are drawn.
    tile_dir, work_dir, z, xs, y0, y1 = args[:6]
    rows = args[6] if len(args) > 6 else range(y0, y1)
    state = _WORKER.get(tile_dir)
    if state is None:
        names = [n for _, n in LAND_BY_ZOOM + SEA_BY_ZOOM] + list(LAKE_RELIEF_ARCHIVES)
        state = (Composite.open(tile_dir, names),
                 Contours(_require_contours(tile_dir)),
                 Water(work_dir))
        _WORKER[tile_dir] = state
    parts, contours, water = state
    out = []
    for x in xs:
        for y in rows:
            data = compose(parts, z, x, y, contours, water)
            if data:
                out.append((z, x, y, data))
    return out


_WORKER: dict = {}


def _marks_path(stage_path: Path, prefix: str) -> Path:
    """Where the per-column send marks live, one set per destination prefix."""
    import hashlib

    if prefix == DEFAULT_PREFIX:
        return stage_path.with_suffix(".sent.sqlite")
    tag = hashlib.sha256(prefix.encode()).hexdigest()[:8]
    return stage_path.with_suffix(f".sent-{tag}.sqlite")


def _stage(path: Path) -> sqlite3.Connection:
    """Staging MBTiles, so an interrupted run resumes instead of restarting."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db.execute(
        "CREATE TABLE IF NOT EXISTS tiles "
        "(zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB, "
        " PRIMARY KEY (zoom_level, tile_column, tile_row))"
    )
    db.execute("CREATE TABLE IF NOT EXISTS done (z INT, col INT, PRIMARY KEY (z, col))")
    return db


def run(
    data_dir: Path | None = None,
    max_zoom: int = MAX_ZOOM,
    *,
    shard: Shard | None = None,
    stage: Path | None = None,
) -> None:
    console.print("\n[bold]Step 08: Flatten to a z/x/y tile tree[/bold]\n")

    from basemap.config import RENDER_WORKERS

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)
    work_dir = get_work_dir(base)
    shard = shard or Shard.from_env()
    ensure_dir(tile_dir)
    stage_path = stage or stage_path_for(base, shard)
    ensure_dir(stage_path.parent)
    # Fail in the first second, not after hours of rendering, and not silently
    # at the end with a tree that is missing a whole layer.
    Water(work_dir)
    _require_contours(tile_dir)
    # The corrected depths are a loose file at the repository root, so a
    # deployment that ships src/ alone renders every corrected lake at its
    # HydroLAKES value and says nothing -- the same shape of failure as the
    # tree that shipped without contours. Counting them here puts it in the
    # first line of the log instead of in twenty-two million tiles.
    corrections = lake_depth_overrides()
    if corrections:
        console.print(f"  {len(corrections)} corrected lake depths")
    else:
        console.print(f"  [yellow]No corrected lake depths: {LAKE_DEPTHS_FILE} "
                      f"is absent, so lakes keep their HydroLAKES values.[/yellow]")
    db = _stage(stage_path)

    # Resume is per column, and a column counts as done even when compose()
    # had nothing to put in it. That is right when a tile genuinely does not
    # exist and wrong the moment the reason it did not exist is fixed: the
    # first run left every ocean column above z8 empty, marked done, and a
    # plain resume would have kept those holes for good. Redoing from a zoom
    # clears the marks at and below it, on both the render and the upload.
    redo = os.environ.get("BASEMAP_XYZ_REDO_FROM")
    if redo:
        from_z = int(redo)
        cleared = db.execute("DELETE FROM done WHERE z >= ?", (from_z,)).rowcount
        # Sharded, the send marks belong to whoever publishes the shards and are
        # not beside this database, so there is nothing here to clear; the
        # publisher takes its own --redo-from.
        resent = 0
        if shard.count == 1:
            marks_path = _marks_path(stage_path, tree_prefix())
            if marks_path.is_file():
                marks = sqlite3.connect(marks_path, isolation_level=None)
                resent = marks.execute("DELETE FROM sent WHERE z >= ?", (from_z,)).rowcount
                marks.close()
        console.print(f"  [yellow]Redoing from z{from_z}: {cleared:,} columns to "
                      f"render again, {resent:,} to send again.[/yellow]")

    done = {tuple(r) for r in db.execute("SELECT z, col FROM done")}

    total = shard.expected_tiles(max_zoom)
    console.print(f"  z0-{max_zoom}: {total:,} tiles at most, "
                  f"{RENDER_WORKERS} workers"
                  + (f", shard {shard.index} of {shard.count}" if shard.count > 1 else "")
                  + (f", resuming ({len(done):,} columns done)" if done else ""))
    # Past max_zoom only where the HR archive has tiles; see DETAIL_ARCHIVE.
    detail: dict[int, dict[int, list[int]]] = {}
    deeper = detail_zooms(max_zoom)
    if deeper:
        archive = detail_archive(tile_dir)
        detail = {z: detail_columns(archive, z, shard) for z in deeper}
    detail_tiles = sum(len(rows) for columns in detail.values() for rows in columns.values())
    if detail:
        console.print(f"  z{deeper[0]}-{deeper[-1]}: {detail_tiles:,} tiles where "
                      f"{DETAIL_ARCHIVE} has them, in "
                      f"{sum(len(c) for c in detail.values()):,} columns")

    def jobs():
        for z in range(max_zoom + 1):
            side = 1 << z
            for x in shard.columns(z):
                if (z, x) not in done:
                    yield (z, x), (tile_dir, work_dir, z, [x], 0, side)
        for z, columns in detail.items():
            for x, rows in columns.items():
                if (z, x) not in done:
                    yield (z, x), (tile_dir, work_dir, z, [x], 0, 0, rows)

    columns_total = (sum(len(shard.columns(z)) for z in range(max_zoom + 1))
                     + sum(len(c) for c in detail.values()))
    pool = ProcessPoolExecutor(max_workers=RENDER_WORKERS)
    written = 0
    with Progress(console=console) as progress, pool:
        task = progress.add_task("Columns", total=columns_total, completed=len(done))
        pending = {}
        queue = jobs()
        while True:
            for key, job in queue:
                pending[pool.submit(_render_batch, job)] = key
                if len(pending) >= RENDER_WORKERS * 2:
                    break
            if not pending:
                break
            ready, _ = wait(pending, return_when="FIRST_COMPLETED")
            for future in ready:
                key = pending.pop(future)
                rows = future.result()
                db.executemany(
                    "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                    [(tz, tx, (1 << tz) - 1 - ty, data) for tz, tx, ty, data in rows],
                )
                db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", key)
                written += len(rows)
                progress.advance(task)
    staged = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
    db.close()
    console.print(f"[green]Staged:[/green] {written:,} tiles in {stage_path}")
    _check_complete(stage_path, staged, shard, max_zoom, detail_tiles)

    # Publishing is part of the step rather than a separate one: a staged tree
    # nobody can reach is not the deliverable. BASEMAP_XYZ_STAGE_ONLY=1 stops
    # after the render, for checking the tiles before they are public.
    if shard.count > 1:
        console.print("  [dim]One shard of several; publishing is a separate step.[/dim]")
        return
    if os.environ.get("BASEMAP_XYZ_STAGE_ONLY"):
        console.print("  [dim]BASEMAP_XYZ_STAGE_ONLY set; not publishing.[/dim]")
        return
    publish(data_dir)


def _check_complete(stage_path: Path, staged: int, shard: Shard, max_zoom: int,
                    detail: int = 0) -> None:
    """Compare what was staged against what the shard owes.

    A shard that renders nothing looks exactly like a shard with nothing to
    render, and that is not a hypothetical: the first flat tree shipped with no
    contours in any of its twenty-two million tiles and nothing said so. The
    count is exact (see Shard.expected_tiles), so this is a real check.

    Hard failure when sharded, because a short shard silently puts a hole in the
    published tree and no later step would notice. Only a warning for the
    whole-tree path, which resumes across runs and interruptions and where a
    short count is a normal intermediate state rather than a defect.
    """
    want = shard.expected_tiles(max_zoom) + detail
    if staged == want:
        return
    message = (f"{stage_path.name}: {staged:,} tiles staged, {want:,} expected "
               f"({want - staged:+,})")
    if detail:
        message += f", {detail:,} of them at the detail zooms"
    if shard.count > 1:
        raise SystemExit(f"{message}\n  This shard is incomplete; do not publish it.")
    console.print(f"  [yellow]{message}[/yellow]")


# Where the tree is written. Publishing 22 million objects takes about as long
# as rendering them, so writing into the live prefix left the public map a mix
# of old and new tiles for most of a day, with a seam moving across it. A
# build writes to a version instead, and the CDN is pointed at it when it is
# complete -- one deployment rather than 22 million copies.
#
# A *version*, deliberately, not a build revision. Versions are few and are
# something a consumer can pin to; one prefix per build would leave a 22
# million object cleanup behind every time and pin to nothing worth pinning
# to. A rebuild that only fixes something republishes the same version; a
# rebuild that changes how the map looks earns the next one.
#
#   basemap/v2/xyz/{z}/{x}/{y}.webp    pinned:  /v2/xyz/{z}/{x}/{y}.webp
#                                      current: /xyz/{z}/{x}/{y}.webp
#
# With no version set the tree goes to the live prefix, which is what a local
# run and every build before this did.
DEFAULT_PREFIX = "basemap/xyz"

# One day, deliberately shorter than S3_CACHE_CONTROL_TILES' week. Switching
# which version /xyz/ serves creates no CloudFront invalidation -- edge copies
# age out on the tile's own max-age -- so this is how long a switch takes to
# reach everyone, and a week is too long to wait to undo a bad one.
CACHE_CONTROL = "public, max-age=86400"


def tree_prefix() -> str:
    """Where this build publishes: the live prefix, or a version under it."""
    explicit = os.environ.get("BASEMAP_XYZ_PREFIX", "").strip("/")
    if explicit:
        return explicit
    version = os.environ.get("BASEMAP_XYZ_VERSION", "").strip("/")
    return f"basemap/{version}/xyz" if version else DEFAULT_PREFIX


def publish(
    data_dir: Path | None = None,
    workers: int | None = None,
    *,
    stages: list[Path] | None = None,
    marks: Path | None = None,
    processes: int | None = None,
    expect_tiles: int | None = None,
) -> None:
    """Write the staged tree to S3 as one object per tile.

    Resume is per column, in a sidecar beside the stage, and keyed by the
    prefix: publishing the same stage to a new prefix must send every column
    again rather than believe the marks from the last one.

    With *stages* given, several shards are published together as one tree --
    the marks sidecar then sits beside the first of them and covers all of them,
    which is sound because a column belongs to exactly one shard.

    *marks* overrides where that sidecar lives, which matters when the shards
    are transient: a publisher that fetches one shard at a time and deletes it
    afterwards must not keep its only record of progress beside them.

    The sending itself lives in pipeline/publish.py, shared with the label tree.
    """
    from basemap.pipeline.publish import publish_stages

    base = get_data_dir(data_dir)
    stage_paths = [Path(s) for s in stages] if stages else [stage_path_for(base)]
    prefix = tree_prefix()
    redo = os.environ.get("BASEMAP_XYZ_REDO_FROM")

    publish_stages(
        stage_paths,
        prefix=prefix,
        marks_path=marks or _marks_path(stage_paths[0], prefix),
        extension="webp",
        content_type="image/webp",
        cache_control=CACHE_CONTROL,
        processes=processes,
        workers=workers,
        redo_from=int(redo) if redo else None,
        expect_tiles=expect_tiles,
    )

    if prefix != DEFAULT_PREFIX:
        console.print("  [yellow]Not live yet.[/yellow] Point the CDN at it with "
                      "deploy/cdn/switch-xyz.py once you have checked it.")
