"""Pipeline step 02c: EMODnet high-resolution coastal DTMs.

The EMODnet ``hr_bathymetry_area`` WFS layer indexes ~1600 survey and LiDAR
grids at 1/32 to 1/1024 arc-minute (~58 m to ~2 m). Each entry links to a zip
containing a SeaDataNet netCDF grid. This step selects the grids intersecting
the configured extent, classifies each into the shared depth zones, and merges
them into a single PMTiles overlay for the highest zoom levels.

Grids are processed individually rather than mosaicked: their footprints are
scattered across Europe, so a common grid would be mostly empty.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np
import rasterio
from rasterio.errors import RasterioError
from rich.console import Console
from rich.progress import Progress

from basemap.config import (
    EMODNET_COVERAGE,
    EMODNET_HR_DOWNLOAD_HOST,
    EMODNET_HR_EXTENT,
    EMODNET_HR_LAYER,
    EMODNET_HR_MAX_ZOOM,
    EMODNET_HR_MIN_DENOM,
    EMODNET_HR_MIN_ZOOM,
    EMODNET_HR_WFS,
    EMODNET_WCS,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.processing.reclassify import reclassify_depth_zones
from basemap.utils.gdal import gdal_polygonize, ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()

Bbox = tuple[float, float, float, float]

# Identifiers come from a remote index and end up in filenames and OGR SQL.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

# Minimum metres of separation before trusting a sign flip.
_SIGN_MARGIN = 1.0


def safe_name(identifier: str) -> str:
    return _SAFE_NAME.sub("_", identifier)[:80]


@dataclass(frozen=True)
class HrDataset:
    identifier: str
    resolution: int
    release: int
    url: str
    bounds: Bbox

    @property
    def name(self) -> str:
        return safe_name(self.identifier)


def is_trusted_url(url: str) -> bool:
    """Only fetch from the EMODnet download host over HTTPS."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == EMODNET_HR_DOWNLOAD_HOST


def geometry_bounds(geometry: dict) -> Bbox:
    xs: list[float] = []
    ys: list[float] = []

    def walk(node) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        for child in node:
            walk(child)

    walk(geometry["coordinates"])
    return (min(xs), min(ys), max(xs), max(ys))


def intersects(a: Bbox, b: Bbox) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def select_datasets(
    features: list[dict],
    extent: Bbox = EMODNET_HR_EXTENT,
    min_denom: int = EMODNET_HR_MIN_DENOM,
) -> list[HrDataset]:
    """Pick usable, in-extent grids from the index, one entry per download."""
    chosen: dict[str, HrDataset] = {}
    for feature in features:
        props = feature.get("properties") or {}
        url = props.get("download_url")
        if not url or not is_trusted_url(url) or url in chosen:
            continue
        if int(props.get("resolution") or 0) < min_denom:
            continue
        bounds = geometry_bounds(feature["geometry"])
        if not intersects(bounds, extent):
            continue
        chosen[url] = HrDataset(
            identifier=str(props.get("identifier") or "unnamed"),
            resolution=int(props["resolution"]),
            release=int(props.get("release") or 0),
            url=url,
            bounds=bounds,
        )
    # Coarsest first so finer grids are drawn over them.
    return sorted(chosen.values(), key=lambda d: d.resolution)


def fetch_index(work: Path, *, timeout: float = 180) -> list[dict]:
    """Download (and cache) the HR bathymetry footprint index."""
    cached = work / "hr_index.geojson"
    if not cached.is_file():
        url = (
            f"{EMODNET_HR_WFS}?service=WFS&version=2.0.0&request=GetFeature"
            f"&typeNames={EMODNET_HR_LAYER}&outputFormat=application/json"
        )
        console.print("  [cyan]Fetching HR bathymetry index[/cyan]")
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        cached.write_bytes(response.content)
    return json.loads(cached.read_text())["features"]


def extract_grid(archive: Path, dest_dir: Path, name: str) -> Path | None:
    """Extract the netCDF grid from an ``.emo.zip``, ignoring archive paths."""
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".nc")]
        if not members:
            return None
        dest = dest_dir / f"{name}.nc"
        if dest.is_file():
            return dest
        with zf.open(members[0]) as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
    return dest


def _reference_median(bounds: Bbox, timeout: float = 120) -> float | None:
    """Median elevation from the EMODnet DTM over *bounds*, for sign checks."""
    west, south, east, north = bounds
    url = (
        f"{EMODNET_WCS}?service=WCS&version=2.0.1&request=GetCoverage"
        f"&coverageId={EMODNET_COVERAGE}"
        f"&subset=Lat({south:g},{north:g})&subset=Long({west:g},{east:g})"
        f"&format=image/tiff"
    )
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    if not response.headers.get("content-type", "").startswith("image/tiff"):
        return None

    with rasterio.MemoryFile(response.content) as memfile, memfile.open() as ds:
        values = ds.read(1, masked=True).astype("float32").filled(np.nan)
    return _nanmedian(values)


def _nanmedian(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if finite.size else None


def detect_sign(nc_uri: str, bounds: Bbox, *, max_samples: int = 256) -> int:
    """Return -1 when a grid stores depth positive-down, else 1.

    EMODnet HR grids are inconsistent: some follow the DTM elevation convention
    (negative below sea level), others store positive depths despite identical
    netCDF metadata. Compare a coarse read against the reference DTM instead of
    trusting the attributes.
    """
    with rasterio.open(nc_uri) as ds:
        step = max(1, max(ds.width, ds.height) // max_samples)
        sample = ds.read(
            1,
            out_shape=(max(1, ds.height // step), max(1, ds.width // step)),
        ).astype("float32")

    grid_median = _nanmedian(sample)
    reference = _reference_median(bounds)
    if grid_median is None or reference is None:
        return 1

    as_is = abs(grid_median - reference)
    flipped = abs(-grid_median - reference)
    return -1 if flipped + _SIGN_MARGIN < as_is else 1


def _process(dataset: HrDataset, work: Path) -> Path | None:
    """Download and vectorize one HR grid. Returns its FlatGeobuf, if any."""
    vectors = ensure_dir(work / "vectors")
    polygons = vectors / f"{dataset.name}.fgb"
    if polygons.is_file():
        return polygons

    archives = ensure_dir(work / "archives")
    grids = ensure_dir(work / "grids")
    rasters = ensure_dir(work / "rasters")

    archive = archives / f"{dataset.name}.zip"
    if not archive.is_file():
        with httpx.stream("GET", dataset.url, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            partial = archive.with_suffix(".part")
            with open(partial, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
        partial.replace(archive)

    try:
        grid = extract_grid(archive, grids, dataset.name)
    except zipfile.BadZipFile:
        console.print(f"  [yellow]{dataset.name}: corrupt archive, skipping[/yellow]")
        return None
    if grid is None:
        console.print(f"  [yellow]{dataset.name}: no netCDF grid in archive[/yellow]")
        return None

    nc_uri = f'NETCDF:"{grid}":elevation'
    classified = rasters / f"{dataset.name}.tif"
    if not classified.is_file():
        sign = detect_sign(nc_uri, dataset.bounds)
        reclassify_depth_zones(nc_uri, classified, sign=sign, crs="EPSG:4326")

    raw = vectors / f"{dataset.name}_raw.fgb"
    gdal_polygonize(classified, raw, layer="depth_zones_hr", field="min_depth")
    ogr2ogr(
        raw, polygons,
        sql=(
            "SELECT min_depth, "
            f"'{dataset.name}' AS source, {dataset.resolution} AS res "
            "FROM depth_zones_hr WHERE min_depth < 0 AND min_depth > -32768"
        ),
        nln="depth_zones_hr",
    )
    raw.unlink(missing_ok=True)
    return polygons


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02c: EMODnet High-Resolution Coastal DTMs[/bold]\n")

    base = get_data_dir(data_dir)
    work = ensure_dir(get_work_dir(base) / "emodnet_hr")
    tile_dir = ensure_dir(get_tile_dir(base))

    pmtiles = tile_dir / "depth_zones_hr.pmtiles"
    if skip_if_exists(pmtiles, "EMODnet HR depth zones"):
        return

    console.print("=== Step 1: Select HR Datasets ===")
    datasets = select_datasets(fetch_index(work))
    if not datasets:
        console.print("[red bold]ERROR:[/] No HR datasets match the configured extent.")
        raise SystemExit(1)
    console.print(f"  [green]{len(datasets)}[/green] grids selected")

    console.print("=== Step 2: Download and Vectorize ===")
    layers: list[Path] = []
    failed = 0
    with Progress(console=console) as progress:
        task = progress.add_task("Processing HR grids", total=len(datasets))
        for dataset in datasets:
            try:
                result = _process(dataset, work)
            except (httpx.HTTPError, RuntimeError, RasterioError) as exc:
                console.print(f"  [yellow]{dataset.name}: {type(exc).__name__}, skipping[/yellow]")
                result = None
            if result is not None:
                layers.append(result)
            else:
                failed += 1
            progress.advance(task)

    if not layers:
        console.print("[red bold]ERROR:[/] No HR grids could be processed.")
        raise SystemExit(1)
    if failed:
        console.print(f"  [yellow]{failed} grid(s) skipped[/yellow]")

    console.print("=== Step 3: Generate PMTiles ===")
    generate_pmtiles(
        layers, pmtiles,
        layer="depth_zones_hr",
        min_zoom=EMODNET_HR_MIN_ZOOM, max_zoom=EMODNET_HR_MAX_ZOOM,
        coalesce_densest=True,
        extra_args=[
            "--simplification=2",
            "--no-feature-limit",
            "--no-tile-size-limit",
            "--detect-shared-borders",
        ],
    )

    console.print(f"[green]Done:[/green] {pmtiles}")
