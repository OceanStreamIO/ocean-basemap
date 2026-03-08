"""Pipeline step 03: Extract depth contours from GEBCO → PMTiles."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.gdal import gdal_contour, gdalwarp, ogr2ogr
from basemap.utils.paths import ensure_dir, resolve_gebco, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 03: Generate Contours[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base) / "contours"
    tile_dir = get_tile_dir(base)
    ensure_dir(work)
    ensure_dir(tile_dir)

    gebco = resolve_gebco(base)

    # Step 1: Downsample for smoothness
    console.print("=== Step 1: Downsample ===")
    downsampled_1km = work / "gebco_1km.tif"
    if not skip_if_exists(downsampled_1km):
        gdalwarp(gebco, downsampled_1km, resolution=0.01, resampling="cubicspline")

    downsampled_500m = work / "gebco_500m.tif"
    if not skip_if_exists(downsampled_500m):
        gdalwarp(gebco, downsampled_500m, resolution=0.005, resampling="cubicspline")

    # Step 2: Extract contours
    console.print("=== Step 2: Extract Contours ===")
    major = work / "contours_major.geojson"
    if not skip_if_exists(major):
        gdal_contour(downsampled_1km, major, interval=1000)

    minor = work / "contours_500m.geojson"
    if not skip_if_exists(minor):
        gdal_contour(downsampled_1km, minor, interval=500)

    shelf = work / "contours_200m.geojson"
    if not skip_if_exists(shelf):
        gdal_contour(downsampled_1km, shelf, interval=200, fixed_levels=[-100, -50])

    shallow = work / "contours_shallow.geojson"
    if not skip_if_exists(shallow):
        gdal_contour(
            downsampled_500m, shallow,
            fixed_levels=[-150, -100, -75, -50, -40, -30, -20, -10],
        )

    # Step 3: Filter ocean-only
    console.print("=== Step 3: Filter Ocean-Only ===")
    contour_sets = {
        "major": major,
        "minor": minor,
        "shelf": shelf,
        "shallow": shallow,
    }
    ocean_files: dict[str, Path] = {}
    for name, src in contour_sets.items():
        dst = work / f"contours_{name}_ocean.geojson"
        if not skip_if_exists(dst):
            ogr2ogr(src, dst, output_format="GeoJSON", where="depth < 0")
        ocean_files[name] = dst

    # Step 4: PMTiles with per-layer zoom ranges
    console.print("=== Step 4: Generate PMTiles ===")
    pmtiles = tile_dir / "depth_contours.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles_multilayer(
            [
                {"file": ocean_files["major"], "layer": "depth_contours", "minzoom": 2},
                {"file": ocean_files["minor"], "layer": "depth_contours", "minzoom": 3},
                {"file": ocean_files["shelf"], "layer": "depth_contours", "minzoom": 4},
                {"file": ocean_files["shallow"], "layer": "depth_contours", "minzoom": 4},
            ],
            pmtiles,
            min_zoom=2, max_zoom=10,
            extra_args=[
                "--simplification=2",
                "--no-feature-limit",
                "--no-tile-size-limit",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
