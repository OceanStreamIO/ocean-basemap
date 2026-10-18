"""Stage Geoscience Australia's AusBathyTopo 30 m regional depth models.

GEBCO's grid around Australia is 460 m; Geoscience Australia compiles every
survey it holds into 30 m regional models -- the Great Barrier Reef, the
Torres Strait, northern Australia and Bass Strait -- published under CC BY 4.0
as cloud-optimised GeoTIFFs inside one zip per region. Elevations are metres
about mean sea level, negative down, on WGS84; land is clamped away by the
render. A 30 m cell is native at z12, so the grids feed both the regional
archive (bathy_gmrt, z9-12) and the HR archive, where they stop at z13.

Two of the records add a notice from the Australian Hydrographic Service --
material reproduced under its licence, not to be copied "without the prior
written consent of the Australian Hydrographic Service" -- beside the CC BY 4.0
licence. Those two are staged only when named explicitly; the default set is
the two whose records carry the licence alone. See NOTICE.

Opt-in by name, like the other staged sources:
``BASEMAP_AUSBATHYTOPO="north torres"``. Each zip is downloaded, its GeoTIFFs
extracted, and the zip deleted.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds
from rich.console import Console

from basemap.config import get_data_dir, get_work_dir
from basemap.processing.raster_tiles import Source
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir

console = Console()

BASE = "https://files.ausseabed.gov.au/survey/"
ATTRIBUTION = "AusBathyTopo 30 m © Commonwealth of Australia (Geoscience Australia), CC BY 4.0"
# name: (zip, catalogue record)
DATASETS: dict[str, tuple[str, str]] = {
    "north": ("North Australia Bathymetry 2018 30m.zip",
              "https://pid.geoscience.gov.au/dataset/ga/121620"),
    "torres": ("Australian Bathymetry Topography (Torres Strait) 2023 30m.zip",
               "https://ecat.ga.gov.au/geonetwork/srv/api/records/9091996a-0173-4dba-b734-1b921d1bbe2d"),
    "gbr": ("Great Barrier Reef Bathymetry 2020 30m.zip",
            "https://pid.geoscience.gov.au/dataset/ga/115066"),
    "bass": ("Bass Strait Bathymetry 2022 30m.zip",
             "https://pid.geoscience.gov.au/dataset/ga/147043"),
}
# Records whose lineage carries the Australian Hydrographic Service's
# written-consent notice beside the CC BY 4.0 licence.
NOTICE = frozenset({"gbr", "bass"})


def wanted() -> list[str]:
    names = os.environ.get("BASEMAP_AUSBATHYTOPO", "").split()
    unknown = sorted(set(names) - set(DATASETS))
    if unknown:
        raise SystemExit(f"no such AusBathyTopo region: {unknown}; have {sorted(DATASETS)}")
    return names


def grids_dir(work: Path) -> Path:
    return work / "ausbathytopo" / "grids"


def stage_one(name: str, work: Path) -> list[Path]:
    """Download one region's zip, extract its GeoTIFFs, delete the zip."""
    dest = ensure_dir(grids_dir(work) / name)
    done = sorted(dest.glob("*.tif"))
    if done:
        return done
    zip_name, _ = DATASETS[name]
    archive = download_file(BASE + zip_name.replace(" ", "%20"),
                            ensure_dir(work / "ausbathytopo") / zip_name, label=zip_name)
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith((".tif", ".tiff"))]
        if not members:
            raise SystemExit(f"{zip_name} holds no GeoTIFF")
        for member in members:
            tmp = dest / (Path(member).name + ".part")
            with zf.open(member) as src, open(tmp, "wb") as out:
                while chunk := src.read(1 << 22):
                    out.write(chunk)
            tmp.replace(dest / Path(member).name)
    archive.unlink()
    return sorted(dest.glob("*.tif"))


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02g: Stage AusBathyTopo[/bold]\n")
    work = get_work_dir(get_data_dir(data_dir))
    names = wanted()
    if not names:
        console.print("  [dim]BASEMAP_AUSBATHYTOPO is unset: nothing to stage.[/dim]")
        return
    for name in names:
        grids = stage_one(name, work)
        size = sum(g.stat().st_size for g in grids) / 1e9
        console.print(f"  {name}: {len(grids)} grids, {size:.1f} GB"
                      + ("  [yellow](hydrographic-service notice; see NOTICE)[/yellow]"
                         if name in NOTICE else ""))
    console.print("[green]Done.[/green]")


def sources(work: Path) -> list[Source]:
    """Staged grids, each capped at the zoom its 30 m cells earn."""
    from basemap.processing.raster_tiles import native_zoom

    out = []
    for grid in sorted(grids_dir(work).glob("*/*.tif")):
        with rasterio.open(grid) as ds:
            box = tuple(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
            cell_m = abs(ds.res[0]) * 111320.0
        out.append(Source(str(grid), bounds=box,
                          max_zoom=native_zoom(cell_m, (box[1] + box[3]) / 2)))
    if out:
        console.print(f"  {len(out)} AusBathyTopo grids")
    return out
