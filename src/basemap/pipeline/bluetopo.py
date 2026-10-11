"""Stage NOAA BlueTopo as local grids for the HR bathymetry stack.

BlueTopo is the US National Bathymetric Source: survey and topo-bathy lidar
compiled at 4 to 16 m, against GEBCO's 460 m and EMODnet's 115 m. It is US
public domain, and it is the only thing of its kind we can use outside Europe.

Three facts about the files decide how this step works.

*It is already cloud-optimised.* The tiles are COGs on AWS open data with
overviews, so band 1 can be read over ``/vsicurl`` without downloading the
whole file. We never hold the raw data: a CONUS pull is 540 GB to 1.1 TB of
three-band float32, and what lands on disk is band 1 alone, under a fifth of
that. Peak disk is the output.

*Most of it is not survey.* Gaps deeper than 200 m are filled from GMRT, which
is coarse and triangulated -- in the Monterey canyon that fill was 96% of an
offshore tile and renders as visible facets. Band 3 indexes a raster attribute
table that grades each contributor, and the fill grades 3 where a survey grades
1. Masked out, those pixels fall through to GEBCO, which is what they came from
and is honest about its resolution.

*Reading it remotely is slow.* A z12 tile took 1.6 s to assemble over the
Atlantic, which is fine for a look and about seven days for a build. Hence
staging rather than streaming at render time.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy
from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_work_dir
from basemap.utils.paths import ensure_dir

console = Console()

BUCKET = "https://noaa-ocs-nationalbathymetry-pds.s3.amazonaws.com"
SCHEME_PREFIX = "BlueTopo/_BlueTopo_Tile_Scheme/"

# A contributor graded anything but 1 is not a survey. 3 is the GMRT fill that
# stands in below 200 m; it is the only other grade seen so far, but the test
# is written as "keep 1" rather than "drop 3" so a new grade cannot slip in.
SURVEYED = 1

WORKERS = int(os.environ.get("BASEMAP_BLUETOPO_WORKERS") or 12)

# A survey older than this is dropped with the fill. The assessment grade does
# not catch these: a 1935 lead-line survey grades 1, the same as 2013 multibeam,
# because the grade is about vertical accuracy and these soundings are accurate.
# What they are not is dense -- hundreds of metres apart, gridded onto 4 m --
# and interpolating them produces speckle and straight track lines across the
# seabed, which is what the first California render showed off Monterey.
# Measured over 60 random CONUS tiles: 11.4% of pixels are pre-1960, against
# 39.5% from 1990 and later.
MIN_SURVEY_YEAR = int(os.environ.get("BASEMAP_BLUETOPO_MIN_SURVEY_YEAR") or 1960)

# And a grid has to carry enough survey to be worth preferring over GEBCO.
# Coverage after masking is bimodal rather than even -- measured across the
# 6,237 CONUS tiles, 36% are under a quarter covered and 39% over three
# quarters -- so a grid kept regardless draws a ribbon of 4 m detail with a
# hard edge across an otherwise 460 m view, which reads as broken rather than
# detailed. Above the threshold a grid carries its whole area; below it, GEBCO
# does, cleanly.
MIN_COVERAGE = float(os.environ.get("BASEMAP_BLUETOPO_MIN_COVERAGE") or 0.6)

# Named by what a reader would call them rather than by UTM zone, because the
# zones straddle the regions: 19 carries both Maine and Puerto Rico.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "conus": (-125.0, 24.0, -66.0, 50.0),
    "alaska": (-180.0, 50.0, -125.0, 72.0),
    "prusvi": (-68.5, 16.0, -64.0, 19.5),
}


def scheme_url() -> str:
    """The newest tile scheme in the bucket. It is republished with a date."""
    listing = urllib.request.urlopen(
        f"{BUCKET}?list-type=2&prefix={SCHEME_PREFIX}", timeout=120
    ).read().decode()
    keys = sorted(re.findall(r"<Key>([^<]+\.gpkg)</Key>", listing))
    if not keys:
        raise SystemExit(f"no tile scheme under {SCHEME_PREFIX}")
    return f"{BUCKET}/{keys[-1]}"


def scheme(work: Path) -> Path:
    """The tile scheme, cached. Six megabytes, and it names every tile."""
    dst = ensure_dir(work / "bluetopo") / "tile_scheme.gpkg"
    if dst.is_file():
        return dst
    url = scheme_url()
    console.print(f"  tile scheme: {url.rsplit('/', 1)[-1]}")
    urllib.request.urlretrieve(url, dst)
    return dst


def tiles_in(work: Path, region: str) -> list[dict]:
    """Published tiles whose centre falls in *region*, finest first."""
    import fiona
    from shapely.geometry import shape

    if region not in REGIONS:
        raise SystemExit(f"no such region: {region}; have {sorted(REGIONS)}")
    w, s, e, n = REGIONS[region]
    out = []
    with fiona.open(scheme(work)) as src:
        for feature in src:
            props = feature["properties"]
            if not props.get("GeoTIFF_Link"):
                continue                      # in the scheme, not yet published
            centre = shape(feature["geometry"]).centroid
            if not (w <= centre.x <= e and s <= centre.y <= n):
                continue
            out.append({
                "tile": props["tile"],
                "url": props["GeoTIFF_Link"],
                "resolution": props.get("Resolution") or "",
                "bounds": shape(feature["geometry"]).bounds,
            })
    out.sort(key=lambda t: (_metres(t["resolution"]), t["tile"]))
    return out


def _metres(resolution: str) -> float:
    """'4m' as a number, for sorting finest first; unknown sorts last."""
    try:
        return float(resolution.rstrip("m"))
    except ValueError:
        return 1e9


def unusable(url: str) -> set[float]:
    """Contributor values to drop: fill, and surveys too old to grid at 4 m.

    Read from the sidecar rather than through GDAL.

    The table lives in ``<tile>.aux.xml`` beside the GeoTIFF. It is a few
    kilobytes and plain XML, where reaching it through the dataset would mean
    GDAL fetching the sidecar for every open.
    """
    try:
        xml = urllib.request.urlopen(f"{url}.aux.xml", timeout=120).read().decode()
    except Exception:                                      # noqa: BLE001
        return set()                                       # no table: keep it all
    columns = [c[1] for c in re.findall(
        r"<FieldDefn index=\"(\d+)\">\s*<Name>([^<]*)</Name>", xml)]
    if "value" not in columns or "data_assessment" not in columns:
        return set()
    bad = set()
    for _, body in re.findall(r"<Row index=\"(\d+)\">(.*?)</Row>", xml, re.S):
        row = dict(zip(columns, re.findall(r"<F>([^<]*)</F>", body)))
        try:
            value = float(row["value"])
        except (KeyError, ValueError):
            continue
        try:
            surveyed = int(row["data_assessment"]) == SURVEYED
        except (KeyError, ValueError):
            surveyed = False
        year = (row.get("survey_date_start") or "")[:4]
        recent = year.isdigit() and int(year) >= MIN_SURVEY_YEAR
        if not (surveyed and recent):
            bad.add(value)
    return bad


def stage_one(entry: dict, dst: Path) -> tuple[int, int]:
    """One tile: band 1, with everything but survey masked out, as a COG.

    Returns the pixels kept and the pixels dropped, so the step can report how
    much of what it pulled was fill.
    """
    url = "/vsicurl/" + entry["url"]
    drop = unusable(entry["url"])
    with rasterio.open(url) as src:
        elevation = src.read(1, masked=True)
        if drop:
            contributor = src.read(3)
            masked = np.isin(contributor, list(drop))
            elevation = np.ma.masked_where(masked, elevation)
        kept = int((~np.ma.getmaskarray(elevation)).sum())
        dropped = int(elevation.size - kept)
        if kept / max(1, kept + dropped) < MIN_COVERAGE:
            # Too little survey to carry its own area. Writing it would put a
            # source in the stack that seeds z12 tiles over its whole rectangle
            # and then supplies a pixel in only part of it, which is the ribbon.
            return 0, dropped
        profile = src.profile | {
            "count": 1, "dtype": "float32", "nodata": float("nan"),
            "driver": "GTiff", "compress": "deflate", "predictor": 3,
            "tiled": True, "blockxsize": 512, "blockysize": 512,
        }
        tmp = dst.with_suffix(".part.tif")
        with rasterio.open(tmp, "w", **profile) as out:
            out.write(np.ma.filled(elevation, np.nan).astype("float32"), 1)
            out.build_overviews([2, 4, 8, 16, 32], rasterio.enums.Resampling.average)
        rio_copy(tmp, dst, driver="COG", COMPRESS="DEFLATE", PREDICTOR=3,
                 BLOCKSIZE=512, OVERVIEWS="FORCE_USE_EXISTING")
        tmp.unlink(missing_ok=True)
    return kept, dropped


def run(data_dir: Path | None = None) -> None:

    console.print("\n[bold]Step 07b: Stage BlueTopo[/bold]\n")
    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    region = os.environ.get("BASEMAP_BLUETOPO_REGION", "conus").strip() or "conus"

    grids = ensure_dir(work / "bluetopo" / "grids")
    entries = tiles_in(work, region)
    console.print(f"  region {region}: {len(entries)} published tiles")

    state_path = work / "bluetopo" / f"staged_{region}.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    # A grid staged under a different cutoff is not the grid this run wants.
    # "It exists" is not "it is current", which is the mistake this pipeline
    # keeps having to relearn.
    stale = [k for k, v in state.items()
             if v.get("min_survey_year") != MIN_SURVEY_YEAR
             or v.get("min_coverage") != MIN_COVERAGE]
    if stale:
        console.print(f"  {len(stale)} grids staged under another cutoff; redoing them")
        for tile in stale:
            state.pop(tile, None)

    todo = [e for e in entries
            if e["tile"] not in state or not (grids / f"{e['tile']}.tif").is_file()]
    console.print(f"  {len(entries) - len(todo)} already staged, {len(todo)} to pull")

    # Each tile is an independent remote read, so this is latency-bound rather
    # than CPU-bound: more workers than cores is the right shape.
    kept_total = dropped_total = 0
    with Progress() as progress:
        task = progress.add_task(f"BlueTopo {region}", total=len(todo))
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(stage_one, e, grids / f"{e['tile']}.tif"): e
                       for e in todo}
            for future in as_completed(futures):
                entry = futures[future]
                progress.advance(task)
                try:
                    kept, dropped = future.result()
                except Exception as exc:                   # noqa: BLE001
                    console.print(f"  [yellow]{entry['tile']}: {exc}[/yellow]")
                    continue
                state[entry["tile"]] = {"kept": kept, "dropped": dropped,
                                        "resolution": entry["resolution"],
                                        "min_survey_year": MIN_SURVEY_YEAR,
                                        "min_coverage": MIN_COVERAGE}
                kept_total += kept
                dropped_total += dropped
                if len(state) % 50 == 0:
                    state_path.write_text(json.dumps(state, indent=1, sort_keys=True))
    state_path.write_text(json.dumps(state, indent=1, sort_keys=True))

    # A grid that no longer qualifies has to leave the disk, not just the
    # state: stage_one returns before writing, so the file from a run under an
    # older rule survives and bluetopo_sources globs it straight back into the
    # stack. "It exists" is not "it is current", again.
    thin = 0
    for tile, entry in state.items():
        if entry["kept"]:
            continue
        thin += 1
        (grids / f"{tile}.tif").unlink(missing_ok=True)

    staged = sorted(grids.glob("*.tif"))
    size = sum(p.stat().st_size for p in staged) / 1024 ** 3
    console.print(f"  {thin} grids held too little survey to carry their area")
    share = dropped_total / max(1, kept_total + dropped_total) * 100
    console.print(f"  [green]{len(staged)}[/green] grids staged, {size:.1f} GB")
    if kept_total or dropped_total:
        console.print(f"  {share:.0f}% of the pixels read were fill rather than survey")
