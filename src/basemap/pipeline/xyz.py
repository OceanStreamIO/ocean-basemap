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


@dataclass
class Composite:
    """The archives open for reading, one process's worth."""

    readers: dict

    @classmethod
    def open(cls, tile_dir: Path, names: list[str]) -> Composite:
        from pmtiles.reader import MmapSource, Reader

        readers = {}
        for name in names:
            path = tile_dir / f"{name}.pmtiles"
            if path.is_file():
                readers[name] = Reader(MmapSource(open(path, "rb")))  # noqa: SIM115
        return cls(readers)

    def tile(self, names: list[str], z: int, x: int, y: int) -> Image.Image | None:
        """The first of *names* that holds this tile, decoded."""
        for name in names:
            reader = self.readers.get(name)
            if reader is None:
                continue
            data = reader.get(z, x, y)
            if data:
                return Image.open(io.BytesIO(data)).convert("RGBA")
        return None


def compose(parts: Composite, z: int, x: int, y: int) -> bytes | None:
    """One opaque tile: land, the shore strip, then the sea over both."""
    land = parts.tile(_pick(LAND_BY_ZOOM, z), z, x, y)
    sea = parts.tile(_pick(SEA_BY_ZOOM, z), z, x, y)
    if land is None and sea is None:
        return None

    if land is not None:
        base = np.asarray(land).astype(np.float32)[..., :3]
    else:
        base = np.zeros((TILE, TILE, 3), dtype=np.float32)
        base[:] = SHORE_WATER

    if sea is not None:
        wet = np.asarray(sea).astype(np.float32)
        alpha = wet[..., 3:4] / 255.0
        # The sea tiles are inset a pixel from the OSM shoreline, so that a
        # coarse archive stretched to a high zoom never tints the land. In the
        # map a vector sea mask fills that strip; here nothing does, and the
        # land colour would show as a hairline along every coast. Growing the
        # sea's own alpha by one pixel covers exactly the strip it left, and
        # the shore colour goes underneath so the grown edge is water rather
        # than whatever the land happened to be.
        grown = _grow(alpha[..., 0])
        shore = np.empty_like(base)
        shore[:] = SHORE_WATER
        base = base * (1.0 - grown[..., None]) + shore * grown[..., None]
        base = base * (1.0 - alpha) + wet[..., :3] * alpha

    out = Image.fromarray(base.round().clip(0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=85, method=4)
    return buf.getvalue()


def _grow(alpha: np.ndarray) -> np.ndarray:
    """Alpha dilated by one pixel: the maximum of each pixel and its neighbours."""
    padded = np.pad(alpha, 1, mode="edge")
    out = alpha.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out = np.maximum(out, padded[dy : dy + TILE, dx : dx + TILE])
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
