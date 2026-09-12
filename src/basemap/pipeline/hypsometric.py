"""Pipeline step 06h: Generate hypsometric color-relief land tiles."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import HYPSOMETRIC_RAMP, get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.gdal import gdal2tiles, gdal_calc, gdaldem_color_relief
from basemap.utils.paths import ensure_dir, resolve_gebco, skip_if_exists

console = Console()


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

    # Step 2: Land-only DEM (ocean → nodata)
    console.print("=== Step 2: Land-Only DEM ===")
    land_dem = work / "gebco_land_only.tif"
    if not skip_if_exists(land_dem):
        gdal_calc(
            gebco, land_dem,
            calc="numpy.where(A >= 0, A, -9999)",
            output_type="Float32",
            nodata=-9999,
        )

    # Step 3: Color-relief with alpha (transparent ocean)
    console.print("=== Step 3: Color-Relief ===")
    color_relief = work / "hypsometric_land.tif"
    if not skip_if_exists(color_relief):
        gdaldem_color_relief(land_dem, ramp_file, color_relief, alpha=True)

    # Step 4: XYZ tiles z0-8
    console.print("=== Step 4: Generate Tiles z0-8 ===")
    if not (tile_dir / "0").exists():
        gdal2tiles(color_relief, tile_dir, zoom="0-8", resampling="lanczos")
    else:
        console.print("  [dim]Tiles already exist, skipping.[/dim]")

    console.print(f"[green]Done:[/green] Hypsometric tiles at {tile_dir}")
