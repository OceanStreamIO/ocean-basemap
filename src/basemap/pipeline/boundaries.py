"""Pipeline step 05d: Natural Earth boundary lines → PMTiles.

The viewer used to take its boundaries from Esri's reference raster, which
bakes country and internal boundaries into the same pixels: they could not be
told apart, restyled, or switched off, and they came out heavy. These are
vector lines in two layers instead --

    countries   admin-0 land boundaries
    regions     admin-1 state and province lines

-- so a federal republic's internal border can be drawn lighter than an
international one, and each can be toggled on its own.

The country polygons in land_base carry boundaries too, but as polygon
outlines: every coast is drawn as a border. These are boundary lines proper,
which stop at the coast.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from rich.console import Console

from basemap.config import (
    BOUNDARY_SHAPEFILES,
    SHORELINE_DETAIL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.utils.download import download_file
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()


def _shapefile(name: str, url: str, work: Path) -> Path:
    shp = work / f"{name}.shp"
    if shp.is_file():
        console.print(f"  [dim]{name} already extracted.[/dim]")
        return shp
    archive = work / f"{name}.zip"
    download_file(url, archive, label=name)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work)
    return shp


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05d: Generate Boundary Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = ensure_dir(get_work_dir(base) / "boundaries")
    tile_dir = ensure_dir(get_tile_dir(base))

    console.print("=== Step 1: Natural Earth boundary lines ===")
    inputs = []
    for layer, (name, url, zooms) in BOUNDARY_SHAPEFILES.items():
        shp = _shapefile(name, url, work)
        geojson = work / f"{layer}.geojson"
        if not skip_if_exists(geojson):
            ogr2ogr(shp, geojson, output_format="GeoJSON")
        inputs.append({"file": geojson, "layer": layer, "minzoom": zooms[0], "maxzoom": zooms[1]})

    console.print("=== Step 2: PMTiles ===")
    pmtiles = tile_dir / "boundaries.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles_multilayer(
            inputs, pmtiles,
            min_zoom=min(i["minzoom"] for i in inputs),
            max_zoom=max(i["maxzoom"] for i in inputs),
            detail=SHORELINE_DETAIL,
            extra_args=[
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--simplification=2",
                "--simplify-only-low-zooms",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
