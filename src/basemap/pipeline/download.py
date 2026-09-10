"""Pipeline step 00: Download GEBCO GeoTIFF tiles and merge."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import (
    CEDA_BASE,
    GEBCO_RELEASE,
    GEBCO_TILES,
    get_data_dir,
    get_gebco_dir,
    get_gebco_tif,
)
from basemap.utils.download import download_file
from basemap.utils.gdal import gdal_merge
from basemap.utils.paths import ensure_dir, skip_if_exists

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print(f"\n[bold]Step 00: Download GEBCO {GEBCO_RELEASE}[/bold]\n")

    base = get_data_dir(data_dir)
    gebco_dir = get_gebco_dir(base)
    ensure_dir(gebco_dir)

    merged = get_gebco_tif(base)
    if skip_if_exists(merged, f"Merged GEBCO {GEBCO_RELEASE} GeoTIFF"):
        return

    # Download tiles
    console.print("[cyan]Downloading 8 GeoTIFF tiles (~0.9 GB each)...[/cyan]")
    tile_paths: list[Path] = []
    for filename, md5 in GEBCO_TILES:
        dest = gebco_dir / filename
        download_file(f"{CEDA_BASE}/{filename}?download=1", dest, checksum=md5, label=filename)
        tile_paths.append(dest)

    # Merge
    console.print(f"\n[cyan]Merging 8 tiles into {merged.name}...[/cyan]")
    gdal_merge(tile_paths, merged)
    console.print(f"[green]Done:[/green] {merged}")
