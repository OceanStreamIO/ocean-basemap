"""Pipeline step 08: the same relief on a geographic grid, for 3D globes.

Every other archive here is Web Mercator, because that is the only grid
MapLibre draws. Mercator cannot represent a pole: it stops at 85.05 degrees,
which leaves out 6.7% of Antarctica -- the South Pole and much of the interior
-- and as much of the Arctic Ocean, the North Pole included. No styling
recovers that; the tiles simply do not exist.

A globe has no such limit, and CesiumJS tiles natively in the geographic grid:
two square tiles at z0, four at z1, reaching 90 degrees at every zoom. These
archives are the same specs as the Mercator ones, rendered on that grid, so a
Cesium viewer can show the sea floor and the ice sheets whole.

    bathy_geo.pmtiles   z0-6   GEBCO, sea only
    land_geo.pmtiles    z0-6   GEBCO relief with the ice ramp, land only

They are deliberately shallow. A globe is looked at from far enough away that
z6 -- about 2 km a pixel at the equator -- covers it, and the Mercator archives
remain the better source anywhere a viewer zooms in past the polar circles.

PMTiles has no field for a projection, and every reader of the format assumes
Mercator, so the grid is recorded in the archive metadata instead (``crs`` and
``scheme``). A client has to know to look; ours does.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.console import Console

from basemap.config import get_data_dir, get_tile_dir
from basemap.pipeline import bathymetry, hypsometric
from basemap.processing.raster_tiles import (
    GEOGRAPHIC,
    RenderSpec,
    finalize,
    render_into,
    stage_path,
)
from basemap.utils.paths import ensure_dir, skip_if_exists

console = Console()

MAX_ZOOM = 6
ATTRIBUTION = "GEBCO Compilation Group; Natural Earth"


def tiles(max_zoom: int = MAX_ZOOM) -> list[tuple[int, int, int]]:
    """Every geographic tile from z0 to *max_zoom*, poles included."""
    return [(z, x, y) for z in range(max_zoom + 1) for x, y in GEOGRAPHIC.world_tiles(z)]


def specs(base: Path) -> dict[str, RenderSpec]:
    """The Mercator specs, moved onto the geographic grid.

    Only the global bands are kept. The regional sources stop at Europe, which
    a globe at these zooms cannot show the detail of anyway, and the HR surveys
    are finer still.
    """
    sea = bathymetry.specs(base)["bathy_global"]
    land = hypsometric.specs(base)["land_relief"]
    return {
        "bathy_geo": replace(
            sea, bands=(sea.band(0),), projection=GEOGRAPHIC
        ),
        "land_geo": replace(
            land, bands=(land.band(0),), projection=GEOGRAPHIC
        ),
    }


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 08: Geographic Grid for Globes[/bold]\n")

    base = get_data_dir(data_dir)
    tile_dir = ensure_dir(get_tile_dir(base))

    for name, spec in specs(base).items():
        console.print(f"=== {name} ===")
        dst = tile_dir / f"{name}.pmtiles"
        if skip_if_exists(dst):
            continue
        render_into(stage_path(dst), spec, tiles(), label=name)
        finalize(
            stage_path(dst), dst, name=name,
            attribution=ATTRIBUTION, projection=GEOGRAPHIC,
        )

    console.print("[green]Done.[/green]")
