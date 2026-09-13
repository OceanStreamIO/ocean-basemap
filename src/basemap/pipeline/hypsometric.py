"""Pipeline step 06h: Generate hypsometric color-relief land tiles."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rich.console import Console

from basemap.config import HYPSOMETRIC_RAMP, get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.gdal import (
    gdal2tiles,
    gdal_calc,
    gdal_rasterize,
    gdaldem_color_relief,
    gdaldem_hillshade,
)
from basemap.utils.paths import ensure_dir, resolve_gebco, skip_if_exists

console = Console()

# 1.0 would drive shadowed slopes to black; this keeps the ramp readable.
SHADE_FLOOR = 0.55


def _bake_relief(color: Path, shade: Path, mask: Path, dst: Path) -> Path:
    """Multiply the color ramp by the hillshade, taking alpha from the land mask.

    Block-windowed because the global grid is ~3.7 gigapixels.
    """
    with rasterio.open(color) as csrc, rasterio.open(shade) as ssrc, rasterio.open(mask) as msrc:
        profile = csrc.profile.copy()
        profile.update(
            count=4, dtype="uint8", nodata=None, tiled=True,
            blockxsize=256, blockysize=256, compress="LZW", BIGTIFF="YES",
        )
        with rasterio.open(dst, "w", **profile) as out:
            for _, win in out.block_windows(1):
                rgb = csrc.read((1, 2, 3), window=win).astype("float32")
                hs = ssrc.read(1, window=win).astype("float32") / 255.0
                land = msrc.read(1, window=win)
                hs = SHADE_FLOOR + (1.0 - SHADE_FLOOR) * hs
                out.write(np.clip(rgb * hs, 0, 255).astype("uint8"), (1, 2, 3), window=win)
                out.write(np.where(land == 1, 255, 0).astype("uint8"), 4, window=win)
    return dst


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 06h: Generate Hypsometric Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base) / "hypsometric"
    tile_dir = get_tile_dir(base) / "hypsometric"
    ensure_dir(work)
    ensure_dir(tile_dir)

    gebco = resolve_gebco(base)

    # Step 1: Color ramp file
    console.print("=== Step 1: Create Color Ramp ===")
    ramp_file = work / "hypsometric_ramp.txt"
    if not ramp_file.exists():
        ramp_file.write_text(HYPSOMETRIC_RAMP)
    console.print(f"  Color ramp: {ramp_file}")

    # Step 2: Clamp the ocean flat. Leaving it as nodata makes gdaldem cut a
    # dark fringe along every coast.
    console.print("=== Step 2: Land DEM (ocean clamped flat) ===")
    land_dem = work / "gebco_land_clamped.tif"
    if not skip_if_exists(land_dem):
        gdal_calc(
            gebco, land_dem,
            calc="numpy.where(A >= 0, A, 0)",
            output_type="Float32",
        )

    # Step 3: Take the shoreline from the OSM coastline rather than from a
    # bathymetry threshold.
    console.print("=== Step 3: Land Mask from OSM Coastline ===")
    osm_land = get_work_dir(base) / "land" / "osm_land.fgb"
    if not osm_land.is_file():
        raise FileNotFoundError(
            f"OSM land polygons not found at {osm_land}. Run the land-tiles step first."
        )
    mask = work / "land_mask.tif"
    if not skip_if_exists(mask):
        with rasterio.open(land_dem) as src:
            bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
            resolution = (src.transform.a, src.transform.e)
        # Without -at only pixels whose centre lies inside a polygon are burned,
        # keeping the mask inside the vector coastline the viewer draws over it.
        gdal_rasterize(
            osm_land, mask,
            bounds=bounds, resolution=resolution,
            label="Rasterize OSM land",
        )

    console.print("=== Step 4: Color-Relief ===")
    color_relief = work / "hypsometric_color.tif"
    if not skip_if_exists(color_relief):
        gdaldem_color_relief(land_dem, ramp_file, color_relief)

    console.print("=== Step 5: Land Hillshade ===")
    shade = work / "hypsometric_shade.tif"
    if not skip_if_exists(shade):
        gdaldem_hillshade(land_dem, shade)

    # Step 6: The viewer draws this over an opaque vector land fill, which would
    # hide a separate hillshade layer, so the shading has to be baked in here.
    console.print("=== Step 6: Bake Shaded Relief ===")
    baked = work / "hypsometric_land.tif"
    if not skip_if_exists(baked):
        _bake_relief(color_relief, shade, mask, baked)

    console.print("=== Step 7: Generate Tiles z0-10 ===")
    if not (tile_dir / "0").exists():
        gdal2tiles(baked, tile_dir, zoom="0-10", resampling="lanczos")
    else:
        console.print("  [dim]Tiles already exist, skipping.[/dim]")

    console.print(f"[green]Done:[/green] Hypsometric tiles at {tile_dir}")
