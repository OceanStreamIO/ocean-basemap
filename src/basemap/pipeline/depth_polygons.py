"""Pipeline step 02: Generate vector depth zone polygons from GEBCO."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.processing.clip_land import clip_to_coastline, require_osm_land
from basemap.processing.reclassify import reclassify_depth_zones
from basemap.utils.gdal import gdal_polygonize, gdalwarp, ogr2ogr
from basemap.utils.paths import ensure_dir, resolve_gebco, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02: Generate Depth Polygons[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = get_tile_dir(base)
    ensure_dir(work)
    ensure_dir(tile_dir)

    gebco = resolve_gebco(base)

    # Step 1: Reclassify
    console.print("=== Step 1: Classify GEBCO into Depth Zones ===")
    classified = work / "gebco_depth_zones.tif"
    if not skip_if_exists(classified):
        reclassify_depth_zones(gebco, classified)

    # Step 2: Downsample + polygonize
    console.print("=== Step 2: Polygonize Depth Zones ===")
    polygons = work / "depth_zones.fgb"
    if not skip_if_exists(polygons):
        downsampled = work / "gebco_depth_zones_220m.tif"
        if not skip_if_exists(downsampled):
            gdalwarp(classified, downsampled, resolution=0.002, resampling="near")

        raw_polygons = work / "depth_zones_raw.fgb"
        gdal_polygonize(downsampled, raw_polygons, layer="depth_zones", field="min_depth")

        # Filter: ocean only (exclude land=1 and nodata=-32768)
        ogr2ogr(raw_polygons, polygons, where="min_depth < 0 AND min_depth > -32768")
        raw_polygons.unlink(missing_ok=True)

    # Step 2b: Clip back to the OSM coastline
    console.print("=== Step 2b: Clip to OSM Coastline ===")
    clipped = work / "depth_zones_clipped.fgb"
    if not skip_if_exists(clipped):
        clip_to_coastline(
            polygons, clipped, require_osm_land(work),
            layer="depth_zones", extent=(-180, -90, 180, 90), cell_size=2.0,
        )

    # Step 3: PMTiles
    console.print("=== Step 3: Generate PMTiles ===")
    pmtiles = tile_dir / "depth_zones.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles(
            clipped, pmtiles,
            layer="depth_zones",
            min_zoom=0, max_zoom=10,
            coalesce_densest=True,
            extra_args=[
                "--simplification=2",
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--detect-shared-borders",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
