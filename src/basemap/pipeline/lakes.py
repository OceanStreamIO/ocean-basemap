"""Pipeline step 05c: HydroLAKES → lake polygons as PMTiles.

The lakes were Natural Earth's 1:10M outlines: a few thousand coarse shapes,
drawn in one flat blue. HydroLAKES has 1.4 million lakes with accurate
shorelines, and carries each lake's mean depth and surface elevation, so lakes
can be coloured on the same depth ramp as the sea instead of one flat tone.

Lakes are tiled in bands by area, so a zoomed-out view carries the few hundred
lakes big enough to see rather than every pond on the planet:

    z2-4    >= 1000 km2     178 lakes
    z5-6    >= 100 km2      1,708
    z7-8    >= 10 km2       16,689
    z9-10   >= 1 km2        185,181
    z11-12  all             1,427,688

The depth in these attributes is a modelled mean, one number per lake. Real
bathymetry within the larger lakes is a separate matter -- GEBCO already
carries a surveyed lake bed for some of them.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from rich.console import Console

from basemap.config import (
    HYDROLAKES_URL,
    LAKE_BANDS,
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

# The attributes worth carrying into the tiles, renamed to the style's casing.
FIELDS = (
    "Lake_name AS name",
    "Lake_area AS area_km2",
    "Depth_avg AS depth_avg",
    "Elevation AS elev_m",
)


def source_shapefile(work: Path) -> Path:
    """Download and unpack HydroLAKES, returning its shapefile."""
    lakes = ensure_dir(work / "lakes")
    shp = lakes / "HydroLAKES_polys_v10_shp" / "HydroLAKES_polys_v10.shp"
    if shp.is_file():
        console.print("  [dim]HydroLAKES already extracted.[/dim]")
        return shp
    archive = lakes / "HydroLAKES_polys_v10_shp.zip"
    download_file(HYDROLAKES_URL, archive, label="HydroLAKES (~820 MB)")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(lakes)
    return shp


def band_file(shp: Path, work: Path, min_area: float) -> Path:
    """The lakes at least *min_area* km2 across, as FlatGeobuf."""
    name = "all" if min_area <= 0 else f"{min_area:g}"
    out = work / "lakes" / f"lakes_{name}.fgb"
    if not skip_if_exists(out):
        where = "" if min_area <= 0 else f" WHERE Lake_area >= {min_area}"
        ogr2ogr(
            shp, out, output_format="FlatGeobuf", nln="lakes",
            sql=f"SELECT {', '.join(FIELDS)} FROM HydroLAKES_polys_v10{where}",
        )
    return out


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05c: Generate Lake Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = ensure_dir(get_tile_dir(base))

    console.print("=== Step 1: HydroLAKES ===")
    shp = source_shapefile(work)

    console.print("=== Step 2: Bands by lake area ===")
    inputs = []
    for min_area, (lo, hi) in LAKE_BANDS:
        inputs.append({
            "file": band_file(shp, work, min_area), "layer": "lakes",
            "minzoom": lo, "maxzoom": hi,
        })

    console.print("=== Step 3: PMTiles ===")
    pmtiles = tile_dir / "lakes.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles_multilayer(
            inputs, pmtiles,
            min_zoom=LAKE_BANDS[0][1][0], max_zoom=LAKE_BANDS[-1][1][1],
            detail=SHORELINE_DETAIL,
            extra_args=[
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--detect-shared-borders",
                "--simplification=2",
                "--simplify-only-low-zooms",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
