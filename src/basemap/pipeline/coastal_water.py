"""Pipeline step: observed water → PMTiles, for the map to draw.

Water the polygons do not have, from the Global Surface Water occurrence
raster. It began as the coast's: OSM's coastline runs along the outer shore of
a barrier island, so the land polygons every other mask here is cut from call
the water behind it land -- the Venice lagoon, the Wadden Sea, the Danube
limans, Ria Formosa. It now covers inland water too: river beds, which nothing
else draws -- HydroLAKES has no rivers, and the network is a centreline -- and
reservoir shores finer than HydroLAKES' polygons (see Water.coastal in
pipeline/xyz.py for why it was ever only the coast). The archive keeps its old
name, coastal_water, because the viewer, the version manifest and the promote
step all know it by that.

The tile tree paints the same raster while it renders. This step publishes it
as an archive, so the map can draw it too. With inland water it is many times
the coastal archive's size and time: run it on the cluster
(deploy/hpc/water.sh), not in a pod.

This is the cheap half of the fix. The honest one is to cut the land polygons
against this raster where they are built, so that sea_mask and the bathymetry's
own sea/land alpha agree with it and every product inherits the correction --
including the tile tree's coastline, which still follows OSM around a lagoon it
has painted as water. That invalidates the raster archives, so it waits for the
next full rebuild rather than forcing one.

Two shortcuts make a global pass affordable. The archive starts at
WATER_MIN_ZOOM because below it the lagoons are narrower than a pixel. And each
level only looks at the children of tiles that held water at the level above --
a quadtree prune off the occurrence pyramid, which turns sixteen million z12
tiles into the ones that actually hold water -- a few tens of thousands when it
was only the coast, far more with every river and pond.
"""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import Progress

from basemap.config import RENDER_WORKERS, get_data_dir, get_tile_dir, get_work_dir
from basemap.processing.raster_tiles import finalize, stage_path

console = Console()

ARCHIVE = "coastal_water"
# z12, which is where the source stops carrying information: Global Surface
# Water is a 30 m grid, and a z12 pixel is 156543*cos(37)/2^12 = 30.5 m at the
# Algarve. z13 was tried first, on the reasoning that this is the only raster
# in the water stack that stopped at 12 while the lakes beside it are vectors
# and stay sharp -- but a z13 tile can only be a 2x upsample of the same cells,
# baked at four times the tiles and four times the bytes, and MapLibre's own
# overzoom of the z12 tile shows the same cells. Raise it with
# BASEMAP_COASTAL_WATER_MAX_ZOOM if a sharper resampling is ever worth that.
MAX_ZOOM = int(os.environ.get("BASEMAP_COASTAL_WATER_MAX_ZOOM") or 12)
QUALITY = 85
ATTRIBUTION = "Water: JRC Global Surface Water"

_WORKER: dict = {}


def _water(work: Path):
    """One reader per process, as the renderer does."""
    if "water" not in _WORKER:
        from basemap.pipeline.xyz import Water

        _WORKER["water"] = Water(work, lakes=False)
    return _WORKER["water"]


def _render(job: tuple) -> tuple[int, int, int, bytes | None]:
    """One tile: the same layer the tree paints, written as it is."""
    from basemap.pipeline.xyz import COASTAL_OUTLINE_MIN_ZOOM, coastal_rgba

    work, z, x, y = job
    water = _water(Path(work))
    coverage = water.coastal(z, x, y)
    if coverage is None or not coverage.any():
        return z, x, y, None

    rgba = coastal_rgba(coverage, outline=z >= COASTAL_OUTLINE_MIN_ZOOM,
                        depth=water.depth(z, x, y))
    buf = BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=QUALITY, method=4)
    return z, x, y, buf.getvalue()


def run(data_dir: Path | None = None) -> None:
    from basemap.pipeline.surface_water import WATER_MIN_ZOOM

    console.print("\n[bold]Step: Observed water archive[/bold]\n")
    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    dst = get_tile_dir(base) / f"{ARCHIVE}.pmtiles"
    stage = stage_path(dst)
    stage.unlink(missing_ok=True)

    db = sqlite3.connect(stage)
    db.executescript(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,"
        " tile_row INTEGER, tile_data BLOB,"
        " PRIMARY KEY (zoom_level, tile_column, tile_row));"
    )

    # The whole world at the shallowest level, then only where water was found.
    from basemap.config import render_bbox
    from basemap.processing.raster_tiles import tiles_in_bbox

    box = render_bbox()
    if box is None:
        side = 1 << WATER_MIN_ZOOM
        candidates = [(WATER_MIN_ZOOM, x, y) for x in range(side) for y in range(side)]
    else:
        console.print(f"  preview box {box}")
        candidates = [(WATER_MIN_ZOOM, x, y)
                      for x, y in tiles_in_bbox(box, WATER_MIN_ZOOM)]

    written = 0
    with ProcessPoolExecutor(RENDER_WORKERS) as pool, Progress() as progress:
        for z in range(WATER_MIN_ZOOM, MAX_ZOOM + 1):
            task = progress.add_task(f"  z{z}", total=len(candidates))
            wet: list[tuple[int, int, int]] = []
            jobs = ((str(work), *t) for t in candidates)
            for tz, tx, ty, blob in pool.map(_render, jobs, chunksize=64):
                progress.advance(task)
                if blob is None:
                    continue
                wet.append((tz, tx, ty))
                db.execute(
                    "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                    (tz, tx, (1 << tz) - 1 - ty, blob),
                )
                written += 1
            db.commit()
            console.print(f"  z{z}: {len(wet):,} tiles with water of {len(candidates):,} looked at")
            if z == MAX_ZOOM:
                break
            # Only the children of wet tiles can hold water at the next level.
            # Sorted down each column: the reader caches a strip of tiles in a
            # column, and in quadtree order every other tile missed it and
            # warped the whole strip again.
            candidates = sorted((z + 1, 2 * x + dx, 2 * y + dy)
                                for _, x, y in wet for dx in (0, 1) for dy in (0, 1))
            if not candidates:
                break
    db.close()

    if not written:
        console.print("[yellow]No observed water found; not writing an archive.[/yellow]")
        return
    finalize(stage, dst, name=ARCHIVE, attribution=ATTRIBUTION)
    console.print(f"[green]Done:[/green] {dst} ({written:,} tiles)")
