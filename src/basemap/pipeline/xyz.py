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
SHORE_WATER = (204, 226, 245)

# Which archive supplies each half of the picture, by zoom. Both lists are
# tried in order and the first that has the tile wins, so a zoom the regional
# archive does not reach falls back to the global one.
LAND_BY_ZOOM = [(11, "land_relief_eu"), (0, "land_relief")]
SEA_BY_ZOOM = [(12, "bathy_hr"), (9, "bathy_eu"), (0, "bathy_global")]


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
# is a thicket. What is drawn here is every multiple of 500 m, with the
# 1000 m multiples given more weight -- fifteen levels, the scheme reviewed
# and chosen as "option 2".
CONTOUR_ARCHIVE = "depth_contours"
CONTOUR_SOURCE_MAX_ZOOM = 10          # the archive's deepest level
CONTOUR_STEP = 500
CONTOUR_MAJOR_STEP = 1000
# The shelf is shallower than the first 500 m line, so on a coastal tile that
# scheme draws nothing at all -- measured over the Peru-Chile margin at z10,
# the whole shelf came out blank. The archive's shallow set fills it, but at
# low zoom those nine lines crowd every coast in the world into a smear, so
# they start where they become legible.
CONTOUR_SHALLOW = frozenset({-10, -20, -30, -40, -50, -75, -100, -150, -200})
CONTOUR_SHALLOW_MIN_ZOOM = 9
CONTOUR_RGB = (66, 106, 148)
# Coverage, which sets how much of the contrast below a line actually gets.
# Measured against the full viewer, which draws the same geometry as vectors and
# is the thing these tiles are asked to look like: #4878a8 at 0.50 opacity for a
# major and 0.42 for a minor, which over pale shelf water is 46 and 39 luminance
# levels of contrast. At 64 and 104 these tiles gave 18 and 30 -- a bit over a
# third -- which is why the lines read as washed out at every zoom, and why they
# were weakest in the one channel a colour-blind reader has to rely on.
# Raised 2026-10-09 to the weight chosen in the viewer's panel: contours at
# 0.64, against the 0.50 the major line's 161 was matched to, so both scale by
# 1.28 and a major lands on the panel's weight exactly.
#
# Scaling each from its own base instead -- 0.42 for a minor, 0.50 for a major
# -- is what the panel literally asks for, because its one slider flattens the
# two to the same opacity. It also puts a minor line at 209 against a major at
# 206, which inverts the hierarchy: the thousand-metre lines would read fainter
# than the lines between them. One scale for both keeps that from happening,
# and keeps the gap the renderer was tuned with.
CONTOUR_MINOR_ALPHA = 175
CONTOUR_MAJOR_ALPHA = 206

# Rec. 709 luminance of CONTOUR_RGB. The whole difficulty is in this number: it
# was chosen against the pale shelf, where it separates well, and the abyssal
# plain sits at about 84 -- so on open ocean the line and the water were the
# same brightness and the contours all but vanished.
CONTOUR_RGB_LUMINANCE = 101
# How far, in luminance, a contour must sit from the water it crosses. The
# weights above then scale it: a major line gets 161/255 of this and a minor
# 137/255. Set so that a deep-water line reaches about the same contrast the
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
        self._geom: list[tuple[bool, np.ndarray]] = []

    def _decode(self, z: int, x: int, y: int) -> list[tuple[bool, np.ndarray]]:
        import gzip

        import mapbox_vector_tile

        if not self._archive.covers(z, *_tile_bounds(z, x, y)):
            return []
        raw = self._archive.get(z, x, y)
        if not raw:
            return []
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        # z is the source zoom, which is min(output zoom, 10); above z10 it is
        # pinned at 10, so gating on it is the same as gating on the output.
        shallow = z >= CONTOUR_SHALLOW_MIN_ZOOM
        out = []
        for layer in mapbox_vector_tile.decode(raw).values():
            extent = layer.get("extent") or 4096
            for feature in layer["features"]:
                depth = feature["properties"].get("depth")
                if depth is None:
                    continue
                if depth % CONTOUR_STEP and not (shallow and depth in CONTOUR_SHALLOW):
                    continue
                geometry = feature["geometry"]
                parts = ([geometry["coordinates"]] if geometry["type"] == "LineString"
                         else geometry["coordinates"])
                major = depth % CONTOUR_MAJOR_STEP == 0
                for part in parts:
                    if len(part) < 2:
                        continue
                    line = np.asarray(part, dtype=np.float32) / extent
                    line[:, 1] = 1.0 - line[:, 1]          # MVT counts y up
                    out.append((major, line))
        return out

    def lines(self, z: int, x: int, y: int) -> list[tuple[bool, np.ndarray]]:
        """Contours crossing this tile, in supersampled tile pixels."""
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
        for major, line in self._geom:
            points = line * span - origin
            lo = points.min(axis=0)
            hi = points.max(axis=0)
            if hi[0] < -2 * ss or lo[0] > edge or hi[1] < -2 * ss or lo[1] > edge:
                continue
            out.append((major, points))
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
        return _encode(_paint_water(np.array(land, dtype=np.uint8), water, z, x, y))

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
                                on_land=(grown == 0).astype(np.float32)))


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

    The two weights share a tone and differ only in alpha, so they go into one
    mask as two grey levels and come out in one blend. Reviewed against separate
    colours per weight: the two differ by under ten in each channel, which is
    invisible under a line one pixel wide, and it halves the work.

    The tone is taken from the water rather than fixed, and that is the whole
    point of it. A fixed colour was used until it was measured: CONTOUR_RGB sits
    at luminance 100, which reads well against the pale shelf it was chosen on
    and has almost nothing left against the abyssal plain at 84. The line landed
    four to six luminance units from its background there -- invisible, and
    invisible exactly where the contours are the only thing describing the
    shape of the seabed. It was backwards.

    So the line aims to sit CONTOUR_CONTRAST away from whatever it crosses,
    going darker where there is room below and lighter where there is not, and
    the existing weights then separate major from minor as before: 0.63 and
    0.54 of the offset, so about 34 and 29 units of contrast on open ocean and
    46 and 39 against the pale shelf, which is what the vector version of these
    same lines achieves in the full viewer. The flip is spread
    over CONTOUR_SPAN rather than switched at a threshold, because a line that
    changed from dark to light partway along would read as two different lines.

    Contrast is luminance, not hue, which is also what makes it legible to a
    reader who cannot separate the colours.
    """
    ss = CONTOUR_SUPERSAMPLE
    mask = Image.new("L", (TILE * ss, TILE * ss), 0)
    pen = ImageDraw.Draw(mask)
    for major, points in lines:
        pen.line(points.ravel().tolist(), width=ss, joint="curve",
                 fill=CONTOUR_MAJOR_ALPHA if major else CONTOUR_MINOR_ALPHA)

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
                 on_land: np.ndarray | None = None) -> np.ndarray:
    """Coastal water, then rivers and lakes over it.

    Order matters: a HydroLAKES lake inside the coastal-water area keeps its
    own ramp, and a river running into a lagoon disappears into it.
    """
    if water is None:
        return base
    # An all-sea tile has nothing to paint, and the read is the most expensive
    # thing in the tile; most of the world is that case.
    if on_land is None or on_land.any():
        base = _draw_coastal(base, water.coastal(z, x, y), on_land)
    lakes, rivers = water.at(z, x, y)
    return _draw_water(base, lakes, rivers, z, x, y)


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
# The tone is a real stop from that ramp -- its colour at 62 m -- and not a
# colour of its own. It was an independent, more saturated blue until the
# Danube delta showed what that costs: measured there, the lagoons rendered at
# luminance 195 while the open sea beside them was 209 to 219, so the inland
# water was the most saturated water on the map and the sea looked washed out
# against it. Taking the tone from the ramp puts a lagoon at the brightness of
# the shelf it opens onto and keeps it there if the ramp is ever resampled.
#
# The edge comes from the occurrence value rather than a threshold. A hard cut
# at 80% turned the lagoon's dendritic channels into blobs; fading between 50
# and 80 keeps them, because a 30 m cell that is half channel and half mudflat
# is genuinely half water.
# The sea ramp's surface stop, not a colour of its own. A lagoon and the shelf
# it opens into are the same water, and the thing that has to differ from both
# is the land: at 228 against land near 215 it does, which the old 2.9%-
# saturated ramp could not manage at any brightness.
COASTAL_WATER_RGB = (204, 226, 245)          # bathymetry-water.txt at 0 m
COASTAL_WATER_LOW = 50.0
COASTAL_WATER_HIGH = 80.0
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
# Rivers are Natural Earth centrelines, the only linear water we hold. They are
# drawn under the lakes so a river meeting a lake disappears into it.
RIVER_RGBA = (128, 176, 205, 165)
RIVER_MIN_ZOOM = 4
WATER_SUPERSAMPLE = 2


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
                 "_cover_key", "_cover", "_want_lakes", "_elev", "_gate_key", "_gate")

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
        river = work / "land" / "rivers_10m.geojson"
        self._rivers_path = str(river) if river.is_file() else None
        if self._rivers_path is None:
            console.print("  [yellow]No rivers_10m.geojson; drawing lakes only.[/yellow]")
        self._key: tuple[int, int, int] | None = None
        self._lakes: list[tuple] = []
        self._rivers: list = []
        self._occurrence = _occurrence_source(work)
        self._cover_key: tuple[int, int, int] | None = None
        self._cover: np.ndarray | None = None
        self._elev = _elevation_source(work)
        self._gate_key: tuple[int, int, int] | None = None
        self._gate: np.ndarray | None = None

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
        if self._rivers_path and z >= RIVER_MIN_ZOOM:
            self._rivers = _lines_in(self._rivers_path, box)

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

    def _read_strip(self, z: int, x: int, gy: int, rows: int | None = None,
                    source=None):
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
        out = np.full((TILE * rows, TILE), np.nan, dtype=np.float32)
        pixel_deg = MERCATOR.pixel_ground_m(z, x, top)[1] / 111320.0
        ds = _dataset(levels.pop(), pixel_deg)
        reproject(
            source=rasterio.band(ds, 1), destination=out,
            src_crs=ds.crs, src_transform=ds.transform, src_nodata=ds.nodata,
            dst_transform=from_bounds(w, s, e, n, TILE, TILE * rows), dst_crs=MERCATOR.crs,
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
        return np.clip((np.nan_to_num(out, nan=0.0) - COASTAL_WATER_LOW)
                       / (COASTAL_WATER_HIGH - COASTAL_WATER_LOW), 0.0, 1.0)


_WATER_CACHE: dict = {}


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


def _lines_in(path: str, lonlat) -> list:
    """River centrelines in a box, in Web Mercator."""
    import shapely

    from basemap.processing.raster_tiles import MERCATOR

    cached = _WATER_CACHE.get(path)
    if cached is None:
        import json

        geoms = []
        for feature in json.loads(Path(path).read_text()).get("features", []):
            geometry = feature.get("geometry")
            if geometry:
                geoms.append(shapely.geometry.shape(geometry))
        every = np.array(geoms, dtype=object)
        cached = _WATER_CACHE[path] = (every, shapely.STRtree(list(every)))
    every, tree = cached
    found = every[tree.query(shapely.box(*lonlat))]
    if not found.size:
        return []
    return list(shapely.transform(found, MERCATOR.to_projected))


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


def _draw_coastal(base: np.ndarray, coverage: np.ndarray | None,
                  on_land: np.ndarray | None) -> np.ndarray:
    """Flat inland-water blue over observed water, on the land side only.

    *on_land* is what keeps this off the sea. The occurrence raster marks the
    coastal ocean as permanent water too, so without the mask this paints flat
    blue over the shelf and erases the bathymetry with it -- which is exactly
    what the first attempt at this did.
    """
    if coverage is None:
        return base
    a = coverage if on_land is None else coverage * on_land
    if not a.any():
        return base
    a = a[..., None]
    colour = np.array(COASTAL_WATER_RGB, dtype=np.float32)
    return (base.astype(np.float32) * (1.0 - a) + colour * a).round().astype(np.uint8)


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

    for line in rivers:
        for part in _parts(line):
            points = to_px(part)
            if len(points) > 1:
                pen.line([c for p in points for c in p], fill=RIVER_RGBA,
                         width=max(1, ss), joint="curve")

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


def _render_batch(args) -> list[tuple[int, int, int, bytes]]:
    tile_dir, work_dir, z, xs, y0, y1 = args
    state = _WORKER.get(tile_dir)
    if state is None:
        names = [n for _, n in LAND_BY_ZOOM + SEA_BY_ZOOM]
        state = (Composite.open(tile_dir, names),
                 Contours(_require_contours(tile_dir)),
                 Water(work_dir))
        _WORKER[tile_dir] = state
    parts, contours, water = state
    out = []
    for x in xs:
        for y in range(y0, y1):
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

    pool = ProcessPoolExecutor(max_workers=RENDER_WORKERS)
    written = 0
    with Progress(console=console) as progress, pool:
        task = progress.add_task("Columns",
                                 total=sum(len(shard.columns(z)) for z in range(max_zoom + 1)),
                                 completed=len(done))
        for z in range(max_zoom + 1):
            side = 1 << z
            columns = [x for x in shard.columns(z) if (z, x) not in done]
            pending = {}
            queue = iter(columns)
            while True:
                for x in queue:
                    job = (tile_dir, work_dir, z, [x], 0, side)
                    pending[pool.submit(_render_batch, job)] = (z, x)
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
    _check_complete(stage_path, staged, shard, max_zoom)

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


def _check_complete(stage_path: Path, staged: int, shard: Shard, max_zoom: int) -> None:
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
    want = shard.expected_tiles(max_zoom)
    if staged == want:
        return
    message = (f"{stage_path.name}: {staged:,} tiles staged, {want:,} expected "
               f"({want - staged:+,})")
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
