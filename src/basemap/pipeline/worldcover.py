"""Pipeline step: ESA WorldCover 2021 at 74 m, for the land-cover tint.

The land relief is tinted by what grows on each pixel (processing/landcover.py),
and this step stages what it reads: the ESA WorldCover 2021 map (v200), a
global 10 m land-cover classification in 2,651 three-degree cloud-optimised
GeoTIFFs on AWS open data. CC BY 4.0, credited as ATTRIBUTION.

Nothing here needs the 10 m pixels. The tint is a per-pixel class and a z12
tile pixel is 38 m, so each tile is fetched at the COG's third overview --
74 m, 4,500 pixels square, a few megabytes against the 70 MB of the full tile
-- and at two coarser overviews for the low zooms. A COG's overviews are
separate images inside the file, so reading one pulls only its own bytes.
The whole world comes to a few gigabytes rather than a few hundred.

Each level is mosaicked into a VRT written here, not by gdalbuildvrt, because
the build Job's image carries GDAL only inside the rasterio wheel. The tiles
are on one grid and do not overlap, so the mosaic is a list of placements.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rasterio
from rasterio.shutil import copy as rio_copy
from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_work_dir, render_bbox
from basemap.processing.raster_tiles import Source
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir

console = Console()

BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
GRID_URL = f"{BASE}/esa_worldcover_grid.fgb"
TILE_URL = f"{BASE}/v200/2021/map/ESA_WorldCover_10m_2021_v200_{{tile}}_Map.tif"
ATTRIBUTION = ("ESA WorldCover 2021 v200, © ESA WorldCover project 2022, "
               "produced by the WorldCover consortium, CC BY 4.0")
# Degrees per pixel of the "10 m" map: 0.3 arc-seconds, 36,000 pixels to a
# three-degree tile, which is 9.3 m at the equator.
NATIVE_DEG = 1.0 / 12000.0
# The COG overview to fetch for each staged level, and the suffix it is
# stored under. The tiles carry six overviews, 2x to 64x; overview n is
# 2^(n+1) times coarser than the map, so these are 74 m, 297 m and 593 m:
# fine enough for z12 (38 m pixels, read at two cover cells per pixel), z9
# and z7 respectively, and the coarsest stands in for everything above.
LEVELS: tuple[tuple[int, str], ...] = ((2, ""), (4, "_ov1"), (5, "_ov2"))
WORKERS = int(os.environ.get("BASEMAP_WORLDCOVER_WORKERS") or 16)


def cover_dir(data_dir: Path) -> Path:
    return get_work_dir(data_dir) / "cover" / "worldcover"


def level_deg(overview: int) -> float:
    return NATIVE_DEG * (2 ** (overview + 1))


def tile_names(dest: Path, bbox: tuple[float, float, float, float] | None = None) -> list[str]:
    """Every tile in the WorldCover grid, or those touching *bbox*.

    The grid file names only the tiles that exist -- the ocean has none -- so
    this is the list to fetch, not every 3-degree cell on earth.
    """
    import fiona
    from shapely.geometry import box, shape

    grid = download_file(GRID_URL, dest / "esa_worldcover_grid.fgb", label="WorldCover grid")
    window = box(*bbox) if bbox else None
    names = []
    with fiona.open(grid) as src:
        for feature in src:
            if window is not None and not shape(feature["geometry"]).intersects(window):
                continue
            names.append(feature["properties"]["ll_tile"])
    return sorted(names)


def tile_path(dest: Path, name: str, suffix: str = "") -> Path:
    return dest / f"{name}{suffix}.tif"


def stage_one(name: str, dest: Path) -> int:
    """Fetch one tile at each staged level. Returns how many files were written."""
    written = 0
    url = f"/vsicurl/{TILE_URL.format(tile=name)}"
    for overview, suffix in LEVELS:
        out = tile_path(dest, name, suffix)
        if out.is_file():
            continue
        tmp = out.with_suffix(".part.tif")
        with rasterio.open(url, OVERVIEW_LEVEL=overview) as src:
            rio_copy(src, tmp, driver="GTiff", COMPRESS="DEFLATE", TILED="YES",
                     BLOCKXSIZE=512, BLOCKYSIZE=512)
        tmp.replace(out)
        written += 1
    return written


def stage(dest: Path, names: list[str], workers: int = WORKERS) -> int:
    ensure_dir(dest)
    todo = [n for n in names
            if not all(tile_path(dest, n, s).is_file() for _, s in LEVELS)]
    console.print(f"  {len(names):,} tiles, {len(todo):,} to fetch")
    written = 0
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                       CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                       GDAL_HTTP_MAX_RETRY=5, GDAL_HTTP_RETRY_DELAY=2)
    with env, Progress(console=console) as progress, ThreadPoolExecutor(workers) as pool:
        task = progress.add_task("WorldCover", total=len(todo))
        for n in pool.map(lambda name: stage_one(name, dest), todo):
            written += n
            progress.advance(task)
    return written


def write_vrt(files: list[Path], out: Path, nodata: int = 0) -> Path:
    """A mosaic of same-grid, non-overlapping tiles, with paths relative to *out*.

    Hand-written because gdalbuildvrt is a command-line tool the build image
    does not have, and because for tiles on one grid a VRT is only a list of
    placements. Relative paths keep it portable between the volume it is
    built on and wherever it is read.
    """
    from xml.sax.saxutils import escape

    if not files:
        raise SystemExit(f"nothing to mosaic into {out}")
    placed = []
    res = wkt = dtype = None
    for path in sorted(files):
        with rasterio.open(path) as ds:
            t = ds.transform
            if res is None:
                res, wkt, dtype = abs(t.a), ds.crs.to_wkt(), ds.dtypes[0]
            elif abs(abs(t.a) - res) > res * 1e-6:
                raise SystemExit(f"{path.name} is not on the mosaic's grid: {abs(t.a)} vs {res}")
            placed.append((path, t.c, t.f, ds.width, ds.height, ds.block_shapes[0]))
    west = min(p[1] for p in placed)
    north = max(p[2] for p in placed)
    east = max(p[1] + p[3] * res for p in placed)
    south = min(p[2] - p[4] * res for p in placed)
    cols, rows = round((east - west) / res), round((north - south) / res)
    gdal_type = {"uint8": "Byte", "uint16": "UInt16", "int16": "Int16",
                 "float32": "Float32"}[dtype]
    lines = [
        f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">',
        f"  <SRS>{escape(wkt)}</SRS>",
        f"  <GeoTransform>{west!r}, {res!r}, 0.0, {north!r}, 0.0, {-res!r}</GeoTransform>",
        f'  <VRTRasterBand dataType="{gdal_type}" band="1">',
        f"    <NoDataValue>{nodata}</NoDataValue>",
        "    <ColorInterp>Gray</ColorInterp>",
    ]
    for path, x0, y0, width, height, (by, bx) in placed:
        rel = os.path.relpath(path, out.parent)
        xoff, yoff = round((x0 - west) / res), round((north - y0) / res)
        lines += [
            "    <SimpleSource>",
            f'      <SourceFilename relativeToVRT="1">{escape(rel)}</SourceFilename>',
            "      <SourceBand>1</SourceBand>",
            f'      <SourceProperties RasterXSize="{width}" RasterYSize="{height}" '
            f'DataType="{gdal_type}" BlockXSize="{bx}" BlockYSize="{by}" />',
            f'      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />',
            f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{width}" ySize="{height}" />',
            "    </SimpleSource>",
        ]
    lines += ["  </VRTRasterBand>", "</VRTDataset>", ""]
    tmp = out.with_suffix(".part.vrt")
    tmp.write_text("\n".join(lines))
    tmp.replace(out)
    return out


def vrt_path(dest: Path, suffix: str = "") -> Path:
    return dest.parent / f"worldcover{suffix}.vrt"


def build_vrts(dest: Path) -> Path:
    """One mosaic per staged level, rewritten whenever the tile set changed."""
    for _, suffix in LEVELS:
        files = sorted(p for p in dest.glob(f"*{suffix}.tif")
                       if (suffix or "_ov" not in p.stem) and not p.name.endswith(".part.tif"))
        write_vrt(files, vrt_path(dest, suffix))
    return vrt_path(dest)


def source(data_dir: Path) -> Source | None:
    """The mosaic as a categorical source, or None when the step has not run."""
    dest = cover_dir(data_dir)
    base = vrt_path(dest)
    if not base.is_file():
        return None
    pyramid = tuple(
        (level_deg(overview), str(vrt_path(dest, suffix)))
        for overview, suffix in LEVELS[1:]
        if vrt_path(dest, suffix).is_file()
    )
    return Source(str(base), valid=(1.0, 100.0), pyramid=pyramid)


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05f: Stage ESA WorldCover[/bold]\n")
    base = get_data_dir(data_dir)
    dest = ensure_dir(cover_dir(base))
    box = render_bbox()
    if box is not None:
        console.print(f"  preview box {box}")
    names = tile_names(dest.parent, box)
    written = stage(dest, names)
    console.print(f"  {written:,} files fetched")
    vrt = build_vrts(dest)
    console.print(f"[green]Done:[/green] {vrt}")
