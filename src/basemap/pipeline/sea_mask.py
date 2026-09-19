"""Pipeline step 05b: OSM land complement → PMTiles for the sea mask.

MapLibre cannot clip a raster to a polygon, so the satellite imagery under the
map is bounded only by whatever opaque layer is drawn on top of it. That used
to be the bathymetry fills, and they are the wrong shape for the job: GEBCO
ends on a 463 m grid, EMODnet on a 115 m one, and neither reaches the OSM
coastline. Clipping them back stopped them overhanging the land, but nothing
filled the strip where they fall short of it -- and that strip is where the
imagery leaked into the water, with the grid's staircase for an edge.

This step tiles the complement of the OSM land polygons. Drawn above the
imagery and below the bathymetry, it covers that strip, and the shoreline
becomes the OSM vector edge at every zoom regardless of what the bathymetry
sources do.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import SHORELINE_DETAIL, get_data_dir, get_tile_dir, get_work_dir
from basemap.processing.clip_land import build_sea_mask, require_osm_land
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05b: Generate Sea Mask[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = get_tile_dir(base)
    ensure_dir(work)
    ensure_dir(tile_dir)

    # Step 1: Complement of the OSM land polygons
    console.print("=== Step 1: Subtract OSM Land ===")
    sea = work / "sea_mask.fgb"
    if not skip_if_exists(sea):
        build_sea_mask(require_osm_land(work), sea, layer="sea")

    # Step 2: PMTiles
    console.print("=== Step 2: Generate PMTiles ===")
    pmtiles = tile_dir / "sea_mask.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles(
            sea, pmtiles,
            layer="sea",
            min_zoom=0, max_zoom=10,
            detail=SHORELINE_DETAIL,
            extra_args=[
                # The whole point of this layer is its edge, so it is the one
                # build that gives up simplification at every zoom rather than
                # only at the maximum.
                "--no-line-simplification",
                "--no-simplification-of-shared-nodes",
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--detect-shared-borders",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
