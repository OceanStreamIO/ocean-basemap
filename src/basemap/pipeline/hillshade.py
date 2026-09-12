"""Pipeline step 01: Generate hillshade raster tiles from GEBCO."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.gdal import gdal2tiles, gdal_calc, gdaldem_hillshade
from basemap.utils.paths import ensure_dir, resolve_gebco, skip_if_exists

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 01: Generate Hillshade[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = get_tile_dir(base) / "hillshade"
    ensure_dir(work)
    ensure_dir(tile_dir)

    gebco = resolve_gebco(base)

    # Step 1: Multi-directional hillshade
    console.print("=== Step 1: Multi-directional Hillshade ===")
    hillshade = work / "hillshade_multi.tif"
    if not skip_if_exists(hillshade):
        gdaldem_hillshade(gebco, hillshade, z_factor=8, multidirectional=True)

    # Step 2: Gamma correction (γ=0.55)
    console.print("=== Step 2: Gamma Correction ===")
    hillshade_gamma = work / "hillshade_gamma.tif"
    if not skip_if_exists(hillshade_gamma):
        gdal_calc(
            hillshade, hillshade_gamma,
            calc="255 * ((A / 255.0) ** 0.55)",
            output_type="Byte",
        )

    # Step 3: Generate XYZ tiles (z0-10)
    console.print("=== Step 3: Generate Tiles z0-10 ===")
    if not (tile_dir / "0").exists():
        gdal2tiles(hillshade_gamma, tile_dir, zoom="0-10", resampling="lanczos")
    else:
        console.print("  [dim]Tiles already exist, skipping.[/dim]")

    console.print(f"[green]Done:[/green] Hillshade tiles at {tile_dir}")
