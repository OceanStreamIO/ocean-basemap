"""Pipeline step 04b: GSFM seamount polygons → PMTiles."""

from __future__ import annotations

import zipfile
from pathlib import Path

from rich.console import Console

from basemap.config import GSFM_URL, get_data_dir, get_tile_dir, get_work_dir
from basemap.processing.clean import clean_seamounts
from basemap.utils.download import download_file
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 04b: Generate Seamounts[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base) / "seamounts"
    tile_dir = get_tile_dir(base)
    ensure_dir(work)
    ensure_dir(tile_dir)

    # Step 1: Download GSFM archive
    console.print("=== Step 1: Download GSFM Shapefiles ===")
    gsfm_zip = work / "gsfm_shapefiles.zip"
    if not skip_if_exists(gsfm_zip):
        download_file(GSFM_URL, gsfm_zip, label="GSFM shapefiles (~389 MB)")

    # Step 2: Extract & find seamount shapefile
    console.print("=== Step 2: Extract & Locate Seamount Shapefile ===")
    gsfm_dir = work / "gsfm_extracted"
    if not gsfm_dir.exists():
        console.print("  Extracting archive...")
        with zipfile.ZipFile(gsfm_zip, "r") as zf:
            zf.extractall(gsfm_dir)

    seamount_shps = list(gsfm_dir.rglob("*[Ss]eamount*.shp"))
    if not seamount_shps:
        console.print("[red]ERROR: No seamount shapefile found in archive.[/red]")
        raise SystemExit(1)
    seamount_shp = seamount_shps[0]
    console.print(f"  Found: {seamount_shp.name}")

    # Step 3: Convert to GeoJSON
    console.print("=== Step 3: Convert to GeoJSON ===")
    seamount_geojson = work / "seamounts.geojson"
    if not skip_if_exists(seamount_geojson):
        ogr2ogr(seamount_shp, seamount_geojson, output_format="GeoJSON", t_srs="EPSG:4326")

    # Step 4: Clean properties
    console.print("=== Step 4: Clean Properties ===")
    seamount_clean = work / "seamounts_clean.geojson"
    if not skip_if_exists(seamount_clean):
        clean_seamounts(seamount_geojson, seamount_clean)

    # Step 5: PMTiles
    console.print("=== Step 5: Generate PMTiles ===")
    pmtiles = tile_dir / "seamounts.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles(
            seamount_clean, pmtiles,
            layer="seamounts",
            min_zoom=0, max_zoom=10,
            extra_args=[
                "--simplification=4",
                "--detect-shared-borders",
                "--no-feature-limit",
                "--no-tile-size-limit",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
