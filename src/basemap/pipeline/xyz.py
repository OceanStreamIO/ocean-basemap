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
from PIL import Image
from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_tile_dir
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
SHORE_WATER = (191, 228, 240)

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


def compose(parts: Composite, z: int, x: int, y: int) -> bytes | None:
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

    raw_sea = parts.raw(names_sea, z, x, y)
    raw_land = parts.raw(names_land, z, x, y)
    if raw_sea is None and raw_land is None:
        return None

    # Open ocean: the sea tile is already the finished picture.
    if raw_land is None and raw_sea is not None:
        sea = Image.open(io.BytesIO(raw_sea))
        if sea.mode == "RGB" or _is_opaque(sea):
            return raw_sea

    land = Image.open(io.BytesIO(raw_land)).convert("RGB") if raw_land else None
    if raw_sea is None:
        buf = io.BytesIO()
        land.save(buf, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        return buf.getvalue()

    wet = np.asarray(Image.open(io.BytesIO(raw_sea)).convert("RGBA"))
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

    buf = io.BytesIO()
    Image.fromarray(base, "RGB").save(buf, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return buf.getvalue()


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


def _render_batch(args) -> list[tuple[int, int, int, bytes]]:
    tile_dir, z, xs, y0, y1 = args
    parts = _WORKER.get(tile_dir)
    if parts is None:
        names = [n for _, n in LAND_BY_ZOOM + SEA_BY_ZOOM]
        parts = Composite.open(tile_dir, names)
        _WORKER[tile_dir] = parts
    out = []
    for x in xs:
        for y in range(y0, y1):
            data = compose(parts, z, x, y)
            if data:
                out.append((z, x, y, data))
    return out


_WORKER: dict = {}


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
    stage_path = ensure_dir(tile_dir) / "xyz.stage.mbtiles"
    db = _stage(stage_path)
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
                    pending[pool.submit(_render_batch, (tile_dir, z, [x], 0, side))] = (z, x)
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


def publish(data_dir: Path | None = None, workers: int = 64) -> None:
    """Write the staged tree to S3 as one object per tile.

    Straight to the public prefix rather than through basemap-build/out and a
    promote: a server-side copy of 22 million objects is 22 million operations,
    and this path is new, so there is nothing live to break while it fills.

    Resume is per column, in a sidecar beside the stage. At this size a run
    will be interrupted -- a pod eviction, a credential expiry, a stall -- and
    starting again from zero would mean re-PUTting tens of millions of objects
    that are already there.
    """
    import time

    import boto3
    from botocore.config import Config

    from basemap.config import S3_BUCKET, S3_ENDPOINT

    base = get_data_dir(data_dir)
    stage_path = get_tile_dir(base) / "xyz.stage.mbtiles"
    if not stage_path.is_file():
        raise SystemExit(f"nothing staged at {stage_path}; run the xyz step first")

    tiles = sqlite3.connect(f"file:{stage_path}?mode=ro", uri=True)
    marks = sqlite3.connect(stage_path.with_suffix(".sent.sqlite"), isolation_level=None)
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
        f"  {total:,} tiles to send → s3://{S3_BUCKET}/basemap/xyz/"
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
            Bucket=S3_BUCKET, Key=f"basemap/xyz/{z}/{col}/{y}.webp", Body=data,
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
    console.print(f"[green]Published:[/green] {sent:,} tiles at {rate:,.0f}/s")
