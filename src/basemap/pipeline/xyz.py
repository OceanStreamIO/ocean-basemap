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

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.paths import ensure_dir

console = Console()

MAX_ZOOM = 12
TILE = 256
WEBP_QUALITY = 85
# method 4 costs about twice method 0 for a percent or two of size; at
# twenty-two million tiles that trade is the wrong way round.
WEBP_METHOD = 1

# The viewer's shore fill (SHORE_WATER in app.js), which has to match the sea
# ramp's surface stop. See color-ramps/bathymetry-water.txt.
SHORE_WATER = (236, 240, 243)

# Which archive supplies each half of the picture, by zoom. Both lists are
# tried in order and the first that has the tile wins, so a zoom the regional
# archive does not reach falls back to the global one.
LAND_BY_ZOOM = [(11, "land_relief_eu"), (0, "land_relief")]
SEA_BY_ZOOM = [(12, "bathy_hr"), (9, "bathy_eu"), (0, "bathy_global")]


def _pick(table: list[tuple[int, str]], z: int) -> list[str]:
    """Archive names to try at *z*, best first."""
    return [name for lo, name in table if z >= lo]


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
CONTOUR_MINOR_ALPHA = 64
CONTOUR_MAJOR_ALPHA = 104
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


# How far above a tile we will look for something to stretch. Four levels is
# a 16x magnification, which is roughly what EMODnet's own deepest tiles are.
MAX_UPSAMPLE = 4


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

    The two weights share a colour and differ only in alpha, so they go into
    one mask as two grey levels and come out in one blend. Reviewed against
    separate colours per weight: the two differ by under ten in each channel,
    which is invisible under a line one pixel wide, and it halves the work.
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
    weight = weight[..., None]
    colour = np.array(CONTOUR_RGB, dtype=np.uint16)
    return ((base.astype(np.uint16) * (255 - weight) + colour * weight)
            // 255).astype(np.uint8)


def _paint_water(base: np.ndarray, water: Water | None, z: int, x: int, y: int,
                 on_land: np.ndarray | None = None) -> np.ndarray:
    """Coastal water, then rivers and lakes over it.

    Order matters: a HydroLAKES lake inside the coastal-water area keeps its
    own ramp, and a river running into a lagoon disappears into it.
    """
    if water is None:
        return base
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
# could not be told apart. EMODnet draw the same water as a flat, saturated
# blue and keep the bathymetric ramp for the open sea, which is why theirs
# reads at a glance.
#
# The edge comes from the occurrence value rather than a threshold. A hard cut
# at 80% turned the lagoon's dendritic channels into blobs; fading between 50
# and 80 keeps them, because a 30 m cell that is half channel and half mudflat
# is genuinely half water.
COASTAL_WATER_RGB = (158, 203, 240)
COASTAL_WATER_LOW = 50.0
COASTAL_WATER_HIGH = 80.0

LAKE_RELIEF_ARCHIVES = ("lake_relief", "lake_relief_coastal")
LAKE_OUTLINE_RGBA = (122, 167, 199, 190)
# Rivers are Natural Earth centrelines, the only linear water we hold. They are
# drawn under the lakes so a river meeting a lake disappears into it.
RIVER_RGBA = (128, 176, 205, 165)
RIVER_MIN_ZOOM = 4
WATER_SUPERSAMPLE = 2


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

    __slots__ = ("_work", "_rivers_path", "_key", "_lakes", "_rivers", "_occurrence")

    def __init__(self, work: Path):
        # Loudly, because the quiet version of this shipped: the first run of
        # the tree had no lakes in it and said nothing, and a missing lake is
        # indistinguishable from a lake that is genuinely not in this tile.
        # Anything that would silently draw no water is an error here instead.
        if not (work / "lakes").is_dir():
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

    def _load(self, z: int, gx: int, gy: int) -> None:
        from basemap.processing.raster_tiles import MERCATOR

        side = 1 << self.GROUP
        w, s, _, _ = MERCATOR.tile_lonlat_bounds(z, gx * side, (gy + 1) * side - 1)
        _, _, e, n = MERCATOR.tile_lonlat_bounds(z, (gx + 1) * side - 1, gy * side)
        pad = (e - w) / TILE                      # a tile pixel, so edges meet
        box = (w - pad, s - pad, e + pad, n + pad)

        band = _lake_band_file(self._work, z)
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
        """Per-pixel coastal-water coverage, 0..1, or None below the floor."""
        from basemap.pipeline.surface_water import WATER_MIN_ZOOM
        from basemap.processing.raster_tiles import water_fraction

        if self._occurrence is None or z < WATER_MIN_ZOOM:
            return None
        frac = water_fraction(self._occurrence, z, x, y)[1:-1, 1:-1]
        return np.clip((frac - COASTAL_WATER_LOW)
                       / (COASTAL_WATER_HIGH - COASTAL_WATER_LOW), 0.0, 1.0)


_WATER_CACHE: dict = {}


def _lakes_in(path: str, lonlat) -> list[tuple]:
    """(geometry, mean depth, surface elevation) for lakes in a box."""
    import fiona
    import shapely
    from shapely.geometry import shape

    from basemap.processing.raster_tiles import MERCATOR

    src = _WATER_CACHE.get(path)
    if src is None:
        src = _WATER_CACHE[path] = fiona.open(path)
    out = []
    for feature in src.filter(bbox=lonlat):
        props = feature["properties"]
        geometry = shape(feature["geometry"])
        if geometry.is_empty:
            continue
        if not geometry.is_valid:
            geometry = shapely.make_valid(geometry)
        out.append((shapely.transform(geometry, MERCATOR.to_projected),
                    float(props.get("depth_avg") or 0.0),
                    float(props.get("elev_m") or 0.0)))
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


def _water_colour(depth: float, elev: float) -> tuple[int, int, int]:
    """A lake's tone: its own ramp inland, the sea's where it meets the sea.

    A lake at sea level sits against the sea and should read like it -- the
    limans by Odesa are the same water as the shelf outside them -- so those
    take the bathymetry ramp at their mean depth. Everything else takes the
    lake ramp, which starts at a clear blue rather than the sea's near-white
    shallows, so even a three-metre pond reads as water against the land.
    """
    from basemap.config import COASTAL_LAKE_ELEV

    ramp = _SEA_RAMP if elev <= COASTAL_LAKE_ELEV else _LAKE_RAMP
    rgb = ramp.colour(np.array([[-abs(depth)]]))[0, 0]
    return int(rgb[0]), int(rgb[1]), int(rgb[2])


def _ramps():
    from basemap.config import RAMP_DIR
    from basemap.processing.raster_tiles import Ramp

    return (Ramp.parse((RAMP_DIR / "lakes.txt").read_text()),
            Ramp.parse((RAMP_DIR / "bathymetry-water.txt").read_text()))


_LAKE_RAMP, _SEA_RAMP = _ramps()


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

    for geometry, depth, elev in lakes:
        fill = (*_water_colour(depth, elev), 255)
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


def run(data_dir: Path | None = None, max_zoom: int = MAX_ZOOM) -> None:
    console.print("\n[bold]Step 08: Flatten to a z/x/y tile tree[/bold]\n")

    from basemap.config import RENDER_WORKERS

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)
    work_dir = get_work_dir(base)
    stage_path = ensure_dir(tile_dir) / "xyz.stage.mbtiles"
    # Fail in the first second, not after hours of rendering, and not silently
    # at the end with a tree that is missing a whole layer.
    Water(work_dir)
    _require_contours(tile_dir)
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
        marks_path = _marks_path(stage_path, tree_prefix())
        resent = 0
        if marks_path.is_file():
            marks = sqlite3.connect(marks_path, isolation_level=None)
            resent = marks.execute("DELETE FROM sent WHERE z >= ?", (from_z,)).rowcount
            marks.close()
        console.print(f"  [yellow]Redoing from z{from_z}: {cleared:,} columns to "
                      f"render again, {resent:,} to send again.[/yellow]")

    done = {tuple(r) for r in db.execute("SELECT z, col FROM done")}

    total = sum(4**z for z in range(max_zoom + 1))
    console.print(f"  z0-{max_zoom}: {total:,} tiles at most, "
                  f"{RENDER_WORKERS} workers"
                  + (f", resuming ({len(done):,} columns done)" if done else ""))

    pool = ProcessPoolExecutor(max_workers=RENDER_WORKERS)
    written = 0
    with Progress(console=console) as progress, pool:
        task = progress.add_task("Columns", total=sum(1 << z for z in range(max_zoom + 1)),
                                 completed=len(done))
        for z in range(max_zoom + 1):
            side = 1 << z
            columns = [x for x in range(side) if (z, x) not in done]
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
    db.close()
    console.print(f"[green]Staged:[/green] {written:,} tiles in {stage_path}")

    # Publishing is part of the step rather than a separate one: a staged tree
    # nobody can reach is not the deliverable. BASEMAP_XYZ_STAGE_ONLY=1 stops
    # after the render, for checking the tiles before they are public.
    if os.environ.get("BASEMAP_XYZ_STAGE_ONLY"):
        console.print("  [dim]BASEMAP_XYZ_STAGE_ONLY set; not publishing.[/dim]")
        return
    publish(data_dir)


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


def tree_prefix() -> str:
    """Where this build publishes: the live prefix, or a version under it."""
    explicit = os.environ.get("BASEMAP_XYZ_PREFIX", "").strip("/")
    if explicit:
        return explicit
    version = os.environ.get("BASEMAP_XYZ_VERSION", "").strip("/")
    return f"basemap/{version}/xyz" if version else DEFAULT_PREFIX


def publish(data_dir: Path | None = None, workers: int = 64) -> None:
    """Write the staged tree to S3 as one object per tile.

    Resume is per column, in a sidecar beside the stage, and keyed by the
    prefix: publishing the same stage to a new prefix must send every column
    again rather than believe the marks from the last one.

    At this size a run will be interrupted -- a pod eviction, a credential
    expiry, a stall -- and starting from zero would mean re-PUTting tens of
    millions of objects that are already there.
    """
    import time

    import boto3
    from botocore.config import Config

    from basemap.config import S3_BUCKET, S3_ENDPOINT

    base = get_data_dir(data_dir)
    stage_path = get_tile_dir(base) / "xyz.stage.mbtiles"
    if not stage_path.is_file():
        raise SystemExit(f"nothing staged at {stage_path}; run the xyz step first")

    prefix = tree_prefix()
    tiles = sqlite3.connect(f"file:{stage_path}?mode=ro", uri=True)
    marks = sqlite3.connect(_marks_path(stage_path, prefix), isolation_level=None)
    marks.execute("CREATE TABLE IF NOT EXISTS sent (z INT, col INT, PRIMARY KEY (z, col))")
    already = {tuple(r) for r in marks.execute("SELECT z, col FROM sent")}

    columns = tiles.execute(
        "SELECT zoom_level, tile_column, count(*) FROM tiles "
        "GROUP BY zoom_level, tile_column ORDER BY zoom_level, tile_column"
    ).fetchall()
    todo = [(z, c, n) for z, c, n in columns if (z, c) not in already]
    total = sum(n for _, _, n in todo)
    done_already = sum(n for z, c, n in columns if (z, c) in already)
    console.print(
        f"  {total:,} tiles to send → s3://{S3_BUCKET}/{prefix}/"
        + (f"  ({done_already:,} already sent)" if already else "")
    )
    if not todo:
        console.print("[green]Nothing to do.[/green]")
        return

    client = boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        config=Config(max_pool_connections=workers * 2, retries={"max_attempts": 5}),
    )

    from concurrent.futures import ThreadPoolExecutor

    def put(row) -> None:
        z, col, row_y, data = row
        y = (1 << z) - 1 - row_y          # MBTiles rows count from the south
        client.put_object(
            Bucket=S3_BUCKET, Key=f"{prefix}/{z}/{col}/{y}.webp", Body=data,
            ContentType="image/webp", CacheControl="public, max-age=86400",
        )

    sent, started = 0, time.time()
    with Progress(console=console) as progress, ThreadPoolExecutor(workers) as pool:
        task = progress.add_task("Tiles", total=total)
        for z, col, n in todo:
            rows = tiles.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ?", (z, col),
            ).fetchall()
            list(pool.map(put, rows))
            marks.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (z, col))
            sent += len(rows)
            progress.advance(task, len(rows))
            if sent % 200_000 < len(rows):
                rate = sent / max(1e-9, time.time() - started)
                console.print(f"  [dim]{sent:,} sent, {rate:,.0f}/s[/dim]")
    rate = sent / max(1e-9, time.time() - started)
    console.print(f"[green]Published:[/green] {sent:,} tiles at {rate:,.0f}/s "
                  f"→ s3://{S3_BUCKET}/{prefix}/")
    if prefix != DEFAULT_PREFIX:
        console.print("  [yellow]Not live yet.[/yellow] Point the CDN at it with "
                      "deploy/cdn/switch-xyz.sh once you have checked it.")
