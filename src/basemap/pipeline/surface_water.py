"""Pipeline step 05d: observed surface water, so lagoons stop being land.

The OSM land polygons every mask here is cut from are built from the OSM
*coastline*, and the coastline runs along the outer barrier islands. Everything
behind it -- the Venetian lagoon, the Wadden flats, a thousand estuaries and
deltas -- falls **inside** the land and is drawn as land, at every zoom.
HydroLAKES does not rescue them either: it excludes water connected to the sea,
so around Venice it holds twenty-six fish-farm basins and not the lagoon.

The depth is not the problem. EMODnet's DTM covers the Venice lagoon at 100%,
down to -40 m, and we already hold it. Only the mask hides it.

Elevation cannot be the test. Measured over Flevoland, **83%** of the land is
below sea level, so "below 0 m is water" drowns the Dutch polder and leaves the
villages as islands; around Venice it is 60%, which makes even the coast that
it gets right come out ragged. Land below sea level is still land.

So the question is answered by observation instead. The JRC Global Surface
Water dataset is 30 m, global, and built from 38 years of Landsat: each pixel
carries the percentage of clear observations in which it was water. A polder is
dry in every scene and stays land. A lagoon is wet in every scene and becomes
water. It picks up the large rivers and reservoirs as well, which is the other
half of the detail EMODnet has and we do not.

    Pekel, Cottam, Gorelick & Belward (2016), "High-resolution mapping of
    global surface water and its long-term changes", Nature 540, 418-422.

504 tiles of 10 degrees, about 20 GB.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_work_dir
from basemap.processing.raster_tiles import Source
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir

console = Console()

GSW_BASE = "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence"
GSW_VERSION = "v1_4_2021"
# The share of clear observations in which a pixel was water. Permanent water
# sits near 100 and a seasonal flood plain well below it; 80 keeps tidal flats
# and the lagoon while leaving paddies, salt pans and river bars as land.
PERMANENT_WATER = 80
# Below this the mask is not consulted. A lagoon is a pixel or two at z6 and
# the read would be the most expensive thing in the tile; the coastline alone
# is the right answer at that scale.
WATER_MIN_ZOOM = 7

# The grid is named for each tile's north-west corner: longitudes every 10
# degrees from 180W to 170E, latitudes from 80N down to 50S. Zero is "0E" and
# "0N" -- "0W" is a 404.
LONGITUDES = tuple(f"{abs(lon)}{'W' if lon < 0 else 'E'}" for lon in range(-180, 180, 10))
LATITUDES = tuple(f"{abs(lat)}{'S' if lat < 0 else 'N'}" for lat in range(80, -60, -10))


def tile_names() -> list[str]:
    return [f"occurrence_{lon}_{lat}{GSW_VERSION}.tif"
            for lat in LATITUDES for lon in LONGITUDES]


def get_water_dir(data_dir: Path) -> Path:
    return get_work_dir(data_dir) / "water" / "gsw"


def download(dest: Path, workers: int = 8) -> list[Path]:
    """Every occurrence tile, skipping the ones already on disk.

    Threaded, because 504 files of 40 MB one at a time is most of an hour on
    one connection and a few minutes on eight. A tile that 404s is not an
    error: the grid is rectangular and the planet is not, so the squares that
    are all ocean or all ice are simply not published.
    """
    ensure_dir(dest)
    names = tile_names()
    got: list[Path] = []
    missing = 0

    def fetch(name: str) -> Path | None:
        path = dest / name
        try:
            return download_file(f"{GSW_BASE}/{name}", path, label=None)
        except Exception:
            return None

    with Progress(console=console) as progress, ThreadPoolExecutor(workers) as pool:
        task = progress.add_task("Tiles", total=len(names))
        for path in pool.map(fetch, names):
            progress.advance(task)
            if path is None:
                missing += 1
            else:
                got.append(path)
    console.print(f"  {len(got):,} tiles on disk, {missing} not published")
    _check_count(dest, len(got))
    return got


def _check_count(dest: Path, found: int) -> None:
    """Hold the tile count steady across hosts.

    Offline, a tile that was never staged and a tile the GSW grid does not
    publish are the same thing -- absent -- and the mask comes out with holes in
    it rather than an error. So the count is recorded on the host that did the
    downloading and asserted everywhere else. This is the surface-water version
    of the check ``Water.__init__`` makes for lakes, and it exists for the same
    reason: a half-staged input must not look like a complete one.
    """
    from basemap.config import OFFLINE

    record = dest / ".tile-count"
    if not OFFLINE:
        record.write_text(f"{found}\n")
        return
    if not record.is_file():
        console.print(f"  [yellow]No .tile-count in {dest}; cannot verify the stage.[/yellow]")
        return
    expected = int(record.read_text().strip())
    if found < expected:
        raise SystemExit(
            f"surface water: {found:,} tiles present but {expected:,} were staged "
            f"({expected - found} lost).\n  in: {dest}\n"
            "  Re-stage the water inputs; a partial set renders a mask with holes."
        )


# The tiles ship with no overviews and 40,000 pixels a side, so a z7 tile would
# read a hundred megapixels of source for a 258 px window. These three take
# 30 m to 240, 480 and 960 m, which covers z7 to z9; from z10 the native grid
# is already fine enough to read directly, and below WATER_MIN_ZOOM the mask is
# not consulted at all.
#
# The fine levels are deliberately absent. Building 2 and 4 as well writes a
# third of the source again in compressed overviews, and measured on this
# volume that was 5 tiles in 20 minutes -- 33 hours for the set. These three
# are about 2% of the source and buy nothing less.
OVERVIEW_LEVELS = (8, 16, 32)
NODATA = 255
NATIVE_DEG = 1 / 3600          # 30 m, as the grid is actually spaced


def add_overviews(tiles: list[Path], workers: int = 16) -> None:
    """Internal overviews, averaged, skipping what is already done.

    The nodata value has to be declared first. The files do not state one, and
    255 means "never observed"; averaged in as a number it would drag a coast
    over the water threshold. Declared, GDAL leaves it out of the average.
    """
    import rasterio
    from rasterio.enums import Resampling

    def build(path: Path) -> bool:
        with rasterio.open(path, "r+") as ds:
            if tuple(ds.overviews(1)) == OVERVIEW_LEVELS:
                return False
            if ds.nodata is None:
                ds.nodata = NODATA
            ds.build_overviews(OVERVIEW_LEVELS, Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
        return True

    built = 0
    # Overviews of a mask do not need to be small, and the default deflate
    # level is most of the cost. GDAL is told to spend its time reading.
    env = rasterio.Env(COMPRESS_OVERVIEW="DEFLATE", ZLEVEL_OVERVIEW=1,
                       GDAL_NUM_THREADS="ALL_CPUS")
    with env, Progress(console=console) as progress, ThreadPoolExecutor(workers) as pool:
        task = progress.add_task("Overviews", total=len(tiles))
        for made in pool.map(build, tiles):
            built += bool(made)
            progress.advance(task)
    console.print(f"  {built:,} built, {len(tiles) - built:,} already had them")


def build_vrt(tiles: list[Path], dest: Path) -> Path:
    """Mosaics over the lot: the native grid and one per overview level."""
    import subprocess

    listing = dest / "gsw.list"
    # Names, not paths, with cwd=dest below: GDAL then writes relativeToVRT="1"
    # and the mosaic travels with its tiles instead of naming this machine.
    listing.write_text("\n".join(p.name for p in sorted(tiles)) + "\n")

    def vrt(level: int | None) -> Path:
        path = dest / ("gsw_occurrence.vrt" if level is None
                       else f"gsw_occurrence_ov{level}.vrt")
        if not path.is_file():
            args = ["gdalbuildvrt", "-q", "-resolution", "highest",
                    "-input_file_list", listing.name]
            if level is not None:
                args += ["-oo", f"OVERVIEW_LEVEL={level}"]
            subprocess.run([*args, path.name], check=True, cwd=dest)
        return path

    for level in range(len(OVERVIEW_LEVELS)):
        vrt(level)
    return vrt(None)


def source(data_dir: Path) -> Source | None:
    """The mosaic as a raster source, or None when the step has not been run.

    Values are the percentage of clear observations in which the pixel was
    water, so *valid* is 0-100 and the 255 that means "never observed" falls
    out as a gap.
    """
    dest = get_water_dir(data_dir)
    base = dest / "gsw_occurrence.vrt"
    if not base.is_file():
        return None
    # -oo OVERVIEW_LEVEL=n indexes the overview list, so level n is coarser
    # than the native grid by OVERVIEW_LEVELS[n].
    pyramid = tuple(
        (NATIVE_DEG * factor, str(dest / f"gsw_occurrence_ov{index}.vrt"))
        for index, factor in enumerate(OVERVIEW_LEVELS)
        if (dest / f"gsw_occurrence_ov{index}.vrt").is_file()
    )
    return Source(str(base), valid=(0.0, 100.0), pyramid=pyramid)


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05d: Global Surface Water[/bold]\n")

    base = get_data_dir(data_dir)
    dest = get_water_dir(base)

    console.print("=== Step 1: Download ===")
    tiles = download(dest)
    if not tiles:
        raise SystemExit("no surface-water tiles downloaded")

    console.print("=== Step 2: Overviews ===")
    add_overviews(tiles)

    console.print("=== Step 3: Mosaic ===")
    vrt = build_vrt(tiles, dest)
    console.print(f"[green]Done:[/green] {vrt}")
