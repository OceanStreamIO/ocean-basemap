"""Pipeline step: observed water as polygons, so its edges stay sharp.

The raster version of this (``coastal_water``) is the same data drawn as
pixels, and it is soft for two reasons that no amount of zoom will fix. Global
Surface Water is a 30.9 m grid, which Web Mercator already matches at z12 --
past that a deeper render magnifies the same cells rather than resolving
anything -- and the raster's alpha deliberately fades across the occurrence
band, because a 30 m cell that is half channel and half mudflat is genuinely
half water.

Drawn as polygons the edge is a vector, so it is crisp at any zoom, exactly
like the HydroLAKES outlines beside it. The cost is a threshold: a polygon is
in or out, and where that line falls is a judgement about what counts as water.
Permanent channels sit near 80% occurrence; drop towards 50% and the tidal
flats, wet for half the year, come in too.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rich.console import Console

from basemap.config import get_data_dir, get_tile_dir, get_work_dir, render_bbox
from basemap.utils.gdal import gdal_polygonize
from basemap.utils.paths import ensure_dir
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()

ARCHIVE = "water_polygons"
LAYER = "water"
MIN_ZOOM = 7
MAX_ZOOM = 13
ATTRIBUTION = "Water: JRC Global Surface Water"


def threshold() -> int:
    """Occurrence at or above which a cell counts as water, as a percentage."""
    return int(os.environ.get("BASEMAP_WATER_THRESHOLD", "80"))


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step: Observed water polygons[/bold]\n")
    base = get_data_dir(data_dir)
    work = ensure_dir(get_work_dir(base) / "water_polygons")
    tile_dir = ensure_dir(get_tile_dir(base))
    dst = tile_dir / f"{ARCHIVE}.pmtiles"

    source = get_work_dir(base) / "water" / "gsw" / "gsw_occurrence.vrt"
    if not source.is_file():
        raise SystemExit(f"missing {source}; run the surface-water step first")

    cut = threshold()
    box = render_bbox()
    console.print(f"  threshold {cut}% occurrence" + (f", box {box}" if box else ", worldwide"))

    mask_path = work / f"water_ge{cut}.tif"
    with rasterio.open(source) as src:
        window = from_bounds(*box, transform=src.transform) if box else None
        data = src.read(1, window=window)
        transform = src.window_transform(window) if window is not None else src.transform
        # GSW stores 0-100 occurrence, with 255 for "no observation". Both the
        # unobserved cells and everything under the threshold are not water.
        wet = ((data >= cut) & (data <= 100)).astype(np.uint8)
        console.print(f"  {int(wet.sum()):,} wet cells of {wet.size:,}")
        profile = src.profile | {
            "driver": "GTiff", "dtype": "uint8", "count": 1, "nodata": 0,
            "height": wet.shape[0], "width": wet.shape[1], "transform": transform,
            "compress": "deflate",
        }
    with rasterio.open(mask_path, "w", **profile) as out:
        out.write(wet, 1)

    polygons = work / f"water_ge{cut}.fgb"
    polygons.unlink(missing_ok=True)
    gdal_polygonize(mask_path, polygons, layer=LAYER, field="wet")

    generate_pmtiles_multilayer(
        [{"file": polygons, "layer": LAYER, "minzoom": MIN_ZOOM}],
        dst,
        min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM,
        # Polygonising a raster gives one square per cell before dissolving, so
        # the counts are large and the shapes are stairs. Tippecanoe's defaults
        # drop features to meet a tile budget, which here would delete channels.
        extra_args=["--no-feature-limit", "--no-tile-size-limit",
                    "--simplification=2", "--detect-shared-borders"],
    )
    console.print(f"[green]Done:[/green] {dst}")
