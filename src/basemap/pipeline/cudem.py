"""Stage NOAA NCEI's CUDEM ninth-arc-second topobathy tiles as HR grids.

BlueTopo is a charting compilation -- NOAA and USACE hydrographic surveys for
navigation -- and research multibeam is outside its remit, which is why
Monterey Canyon stayed soft after BlueTopo went in: the canyon's survey grade
in its attribute table is a generalisation, and the filter that keeps BlueTopo
honest everywhere else rejects it there. NCEI's Continuously Updated DEM
(CUDEM) is the other national compilation, built for inundation modelling, and
it does take the research surveys: the California tiles carry MBARI's canyon
multibeam at 1/9 arc-second, about 3 m, from the shoreline to below 900 m.

The tiles are public-domain cloud-optimised GeoTIFFs on AWS open data, a
quarter degree square with internal overviews, named by their north-west
corner (``ncei19_n36x75_w122x00_2023v1.tif``). Elevations are metres on
NAVD88, positive up, so bathymetry is negative like GEBCO and land is clamped
away by the render. Regions are the bucket's own folders and are opt-in, like
BlueTopo's: ``BASEMAP_CUDEM_REGIONS="CA"``.
"""

from __future__ import annotations

import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_work_dir
from basemap.processing.raster_tiles import Source
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir

console = Console()

BUCKET = "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com"
PREFIX = "dem/NCEI_ninth_Topobathy_2014_8483/"
ATTRIBUTION = ("NOAA NCEI Continuously Updated DEM (CUDEM), 1/9 arc-second, "
               "doi:10.25921/ds9v-ky35")
# The tiles' compound CRS is NAD83 plus NAVD88 heights; read as plain NAD83 so
# the warper does a horizontal transform and nothing else.
HORIZONTAL_CRS = "EPSG:4269"
WORKERS = int(os.environ.get("BASEMAP_CUDEM_WORKERS") or 8)
NAME = re.compile(r"ncei19_n(\d+)x(\d+)_w(\d+)x(\d+)_.*\.tif$")


def wanted_regions() -> list[str]:
    """Folder names in BASEMAP_CUDEM_REGIONS; nothing when it is unset."""
    return os.environ.get("BASEMAP_CUDEM_REGIONS", "").split()


def grids_dir(work: Path) -> Path:
    return work / "cudem" / "grids"


def list_keys(region: str) -> list[tuple[str, int]]:
    """(key, size) for every tile in one region folder, following continuations."""
    keys: list[tuple[str, int]] = []
    token = None
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={PREFIX}{region}/&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.request.quote(token, safe="")
        body = urllib.request.urlopen(url, timeout=120).read().decode()
        for key, size in re.findall(r"<Key>([^<]+\.tif)</Key>.*?<Size>(\d+)</Size>", body):
            keys.append((key, int(size)))
        more = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not more:
            return keys
        token = more.group(1)


def tile_bounds(name: str) -> tuple[float, float, float, float] | None:
    """A tile's box from its name: the name gives the north-west corner."""
    m = NAME.search(name)
    if not m:
        return None
    north = int(m.group(1)) + int(m.group(2)) / 100
    west = -(int(m.group(3)) + int(m.group(4)) / 100)
    return (west, north - 0.25, west + 0.25, north)


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02f: Stage NCEI CUDEM[/bold]\n")
    work = get_work_dir(get_data_dir(data_dir))
    regions = wanted_regions()
    if not regions:
        console.print("  [dim]BASEMAP_CUDEM_REGIONS is unset: nothing to stage.[/dim]")
        return
    for region in regions:
        keys = list_keys(region)
        if not keys:
            raise SystemExit(f"no CUDEM tiles under {PREFIX}{region}/")
        dest = ensure_dir(grids_dir(work) / region)
        total = sum(size for _, size in keys) / 1e9
        console.print(f"  {region}: {len(keys)} tiles, {total:.1f} GB")
        with Progress(console=console) as progress, ThreadPoolExecutor(WORKERS) as pool:
            task = progress.add_task(f"CUDEM {region}", total=len(keys))
            futures = [pool.submit(download_file, f"{BUCKET}/{key}", dest / key.rsplit("/", 1)[-1],
                                   label=None) for key, _ in keys]
            for future in as_completed(futures):
                future.result()
                progress.advance(task)
    console.print("[green]Done.[/green]")


def sources(work: Path) -> list[Source]:
    """Staged CUDEM tiles, each capped at the zoom its 3 m cells earn."""
    from basemap.processing.raster_tiles import native_zoom

    out = []
    for grid in sorted(grids_dir(work).glob("*/*.tif")):
        box = tile_bounds(grid.name)
        if box is None:
            continue
        cell_m = (1 / 9) / 3600 * 111320.0
        out.append(Source(str(grid), crs=HORIZONTAL_CRS, bounds=box,
                          max_zoom=native_zoom(cell_m, (box[1] + box[3]) / 2)))
    if out:
        console.print(f"  {len(out)} CUDEM tiles")
    return out
