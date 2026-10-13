"""Stage GMRT's masked grid as local grids for the bathymetry stack.

GMRT -- the Global Multi-Resolution Topography synthesis, Ryan et al. 2009 --
is the compilation of every ship multibeam swath its curators can gather,
gridded at about 100 m, and it is what the Esri ocean basemap shows wherever
that map is sharp. Its ``topo-mask`` layer holds only the swath-sourced cells
and NaN everywhere else, which is the one thing BlueTopo's attribute table
could not say: where the survey ends and the fill begins. Measured at
Monterey, the masked grid matched Esri's tiles one for one; an open-Pacific
control came back 94% NaN. Masked, it falls through to GEBCO exactly where
GEBCO is all there is.

It is served as a grid API rather than a tile scheme: one HTTP request per
box of at most 20 by 20 degrees, which is the 2 GB request limit at 100 m.
So staging is a short list of named boxes. The 3 by 3.5 degree California box
is 165 MB in 52 s; a full 20 by 20 box is about 1.5 GB.

Licence CC BY 4.0, credited as ATTRIBUTION; the data are not for navigation.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import rasterio
from rasterio.shutil import copy as rio_copy
from rich.console import Console

from basemap.config import get_data_dir, get_work_dir
from basemap.utils.paths import ensure_dir

console = Console()

API = "https://www.gmrt.org/services/GridServer"
ATTRIBUTION = ("GMRT: Ryan et al. (2009), Global Multi-Resolution Topography synthesis, "
               "doi:10.1594/IEDA.100001, CC BY 4.0")
LAYER = "topo-mask"
# Metres per node asked of the service. Its finest is about 100 m; what comes
# back at this setting is 49 m nodes at Monterey, which is the server's own
# grid rather than resampling here.
RESOLUTION_M = int(os.environ.get("BASEMAP_GMRT_RESOLUTION_M") or 100)
# A request may not exceed 2 GB, which the service puts at 20 by 20 degrees.
MAX_SPAN_DEG = 20.0

# Named by coast, not by a grid, so a region reads as what it covers. Each is
# within MAX_SPAN_DEG on both axes; they overlap where the coasts do, which is
# harmless -- the same masked cells come back twice and the first grid wins. Alaska is deliberately absent for now, as
# it is for BlueTopo; it goes in when the Americas are judged.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "us-west":         (-135.0, 30.0, -115.0, 50.0),
    "mexico-pacific":  (-120.0, 10.0, -100.0, 30.0),
    "gulf-of-mexico":  (-100.0, 17.0,  -80.0, 31.0),
    "caribbean-west":   (-90.0,  7.0,  -70.0, 19.0),
    "antilles":         (-78.0,  7.0,  -58.0, 24.0),
    "bahamas-cuba":     (-85.0, 19.0,  -70.0, 32.0),
    "us-northeast":     (-82.0, 32.0,  -62.0, 48.0),
    "hawaii":          (-162.0, 17.0, -153.0, 24.0),
}


def wanted_regions() -> list[str]:
    """Regions named in BASEMAP_GMRT_REGIONS; nothing when it is unset.

    Unset means the step is a no-op rather than a global pull, the same way
    BlueTopo's region works: fetching every box is a decision, not a default.
    """
    names = os.environ.get("BASEMAP_GMRT_REGIONS", "").split()
    unknown = sorted(set(names) - set(REGIONS))
    if unknown:
        raise SystemExit(f"no such GMRT region: {unknown}; have {sorted(REGIONS)}")
    return names


def grids_dir(work: Path) -> Path:
    return work / "gmrt" / "grids"


def check_box(name: str, box: tuple[float, float, float, float]) -> None:
    west, south, east, north = box
    if not (west < east and south < north):
        raise SystemExit(f"GMRT region {name} is inside out: {box}")
    if east - west > MAX_SPAN_DEG or north - south > MAX_SPAN_DEG:
        raise SystemExit(f"GMRT region {name} spans more than {MAX_SPAN_DEG:.0f} degrees: {box}")


def stage_one(name: str, box: tuple[float, float, float, float], dst: Path,
              resolution_m: int = RESOLUTION_M, client: httpx.Client | None = None) -> float:
    """Fetch one box as a COG with NaN declared as nodata. Returns the share of cells with data.

    The service's GeoTIFF carries NaN for the unsurveyed cells but no nodata
    tag, so the warper would read those NaNs as elevation. The tag is set on
    the download in place and the COG is written from that, which is the
    same shape every other staged grid has: deflate, 512-pixel blocks and
    averaged overviews, so a z9 tile reads a few blocks and not a region.
    """
    check_box(name, box)
    west, south, east, north = box
    download = dst.with_suffix(".dl.tif")
    own = client is None
    client = client or httpx.Client(timeout=900, follow_redirects=True)
    try:
        with client.stream("GET", API, params=dict(
                west=west, south=south, east=east, north=north,
                layer=LAYER, format="geotiff", mresolution=resolution_m)) as r:
            r.raise_for_status()
            with open(download, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
    finally:
        if own:
            client.close()
    with open(download, "rb") as fh:
        if fh.read(2) not in (b"II", b"MM"):
            download.unlink(missing_ok=True)
            raise SystemExit(f"GMRT did not return a GeoTIFF for {name}; the service may be down")
    with rasterio.open(download, "r+") as ds:
        ds.nodata = float("nan")
    tmp = dst.with_suffix(".part.tif")
    rio_copy(download, tmp, driver="COG", COMPRESS="DEFLATE", PREDICTOR=3,
             BLOCKSIZE=512, OVERVIEW_RESAMPLING="AVERAGE")
    download.unlink(missing_ok=True)
    tmp.replace(dst)
    with rasterio.open(dst) as ds:
        sample = ds.read(1, out_shape=(min(ds.height, 2048), min(ds.width, 2048)), masked=True)
    return float(1.0 - sample.mask.mean()) if sample.mask is not False else 1.0


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02e: Stage GMRT swath bathymetry[/bold]\n")
    work = get_work_dir(get_data_dir(data_dir))
    names = wanted_regions()
    if not names:
        console.print("  [dim]BASEMAP_GMRT_REGIONS is unset: nothing to stage.[/dim]")
        return
    grids = ensure_dir(grids_dir(work))
    with httpx.Client(timeout=900, follow_redirects=True) as client:
        for name in names:
            dst = grids / f"{name}.tif"
            if dst.is_file():
                console.print(f"  {name}: already staged")
                continue
            started = time.monotonic()
            share = stage_one(name, REGIONS[name], dst, client=client)
            console.print(f"  {name}: {dst.stat().st_size / 1e6:,.0f} MB, "
                          f"{share * 100:.0f}% of cells carry data, {time.monotonic() - started:.0f} s")
            # One shared research service; a pause between gigabyte pulls is manners.
            time.sleep(2.0)
    console.print("[green]Done.[/green]")
