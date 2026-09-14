"""Pipeline step 02b: EMODnet regional depth zones (European seas).

Downloads the EMODnet Bathymetry DTM (~115 m) over the configured extent via
the OGC WCS service, classifies it into the same depth zones as the global
GEBCO layer, and emits a PMTiles overlay for high zoom levels.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress

from basemap.config import (
    EMODNET_CHUNK_DEG,
    EMODNET_COVERAGE,
    EMODNET_EXTENT,
    EMODNET_MAX_ZOOM,
    EMODNET_MIN_ZOOM,
    EMODNET_POLYGONIZE_RES,
    EMODNET_WCS,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.processing.clip_land import clip_to_coastline, require_osm_land
from basemap.processing.reclassify import reclassify_depth_zones
from basemap.utils.gdal import gdal_polygonize, gdalbuildvrt, gdalwarp, ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()

Bbox = tuple[float, float, float, float]


def chunk_bboxes(
    extent: Bbox = EMODNET_EXTENT,
    step: float = EMODNET_CHUNK_DEG,
) -> list[Bbox]:
    """Split *extent* (west, south, east, north) into a grid of request boxes."""
    west, south, east, north = extent
    if step <= 0:
        raise ValueError("step must be positive")

    boxes: list[Bbox] = []
    lat = south
    while lat < north:
        lat_hi = round(min(lat + step, north), 6)
        lon = west
        while lon < east:
            lon_hi = round(min(lon + step, east), 6)
            boxes.append((round(lon, 6), round(lat, 6), lon_hi, lat_hi))
            lon = lon_hi
        lat = lat_hi
    return boxes


def coverage_url(box: Bbox) -> str:
    """WCS 2.0.1 GetCoverage URL for *box*, returning a GeoTIFF."""
    west, south, east, north = box
    return (
        f"{EMODNET_WCS}?service=WCS&version=2.0.1&request=GetCoverage"
        f"&coverageId={EMODNET_COVERAGE}"
        f"&subset=Lat({south:g},{north:g})"
        f"&subset=Long({west:g},{east:g})"
        f"&format=image/tiff"
    )


def chunk_name(box: Bbox) -> str:
    west, south, east, north = box
    return f"emodnet_n{north:g}_s{south:g}_w{west:g}_e{east:g}.tif"


def _fetch_chunk(
    box: Bbox, dest: Path, *, timeout: float = 300, attempts: int = 7
) -> Path | None:
    """Download one WCS chunk. Returns None when the server has no coverage.

    Retries 5xx and transport faults: this runs hundreds of times inside a
    multi-hour pipeline, so a single blip must not discard the whole run.
    The backoff spans ~2 min in total, enough to ride out the 502 bursts the
    gateway throws when it is under load.
    """
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    partial = dest.with_suffix(".part")
    for attempt in range(1, attempts + 1):
        try:
            with httpx.stream(
                "GET", coverage_url(box), timeout=timeout, follow_redirects=True
            ) as r:
                r.raise_for_status()
                # WCS reports errors as an XML body rather than an HTTP status.
                if not r.headers.get("content-type", "").startswith("image/tiff"):
                    detail = r.read()[:200].decode("utf-8", "replace").strip()
                    console.print(
                        f"  [yellow]{dest.stem}: no coverage[/yellow] [dim]{detail}[/dim]"
                    )
                    return None

                with open(partial, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            # A 4xx means the request itself is wrong, so retrying cannot help.
            status = getattr(getattr(exc, "response", None), "status_code", 500)
            if status < 500 or attempt == attempts:
                raise
            delay = min(2**attempt, 60)
            console.print(
                f"  [yellow]{dest.stem}: {type(exc).__name__} ({status}), "
                f"retrying in {delay}s ({attempt}/{attempts - 1})[/yellow]"
            )
            time.sleep(delay)
        else:
            partial.replace(dest)
            return dest
    return None


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 02b: EMODnet Regional Depth Zones[/bold]\n")

    base = get_data_dir(data_dir)
    work = ensure_dir(get_work_dir(base) / "emodnet")
    tile_dir = ensure_dir(get_tile_dir(base))

    pmtiles = tile_dir / "depth_zones_eu.pmtiles"
    if skip_if_exists(pmtiles, "EMODnet depth zones"):
        return

    # Step 1: Download DTM chunks over the configured extent
    boxes = chunk_bboxes()
    console.print(f"=== Step 1: Download EMODnet DTM ({len(boxes)} WCS requests) ===")
    chunks_dir = ensure_dir(work / "chunks")
    rasters: list[Path] = []
    with Progress(console=console) as progress:
        task = progress.add_task("Fetching EMODnet chunks", total=len(boxes))
        for box in boxes:
            fetched = _fetch_chunk(box, chunks_dir / chunk_name(box))
            if fetched:
                rasters.append(fetched)
            progress.advance(task)

    if not rasters:
        console.print("[red bold]ERROR:[/] EMODnet returned no coverage for the extent.")
        raise SystemExit(1)

    # Step 2: Mosaic into a virtual raster
    console.print("=== Step 2: Mosaic Chunks ===")
    vrt = work / "emodnet_mean.vrt"
    gdalbuildvrt(rasters, vrt)

    # Step 3: Reclassify into depth zones
    console.print("=== Step 3: Classify into Depth Zones ===")
    classified = work / "emodnet_depth_zones.tif"
    if not skip_if_exists(classified):
        reclassify_depth_zones(vrt, classified)

    # Step 4: Polygonize
    console.print("=== Step 4: Polygonize Depth Zones ===")
    polygons = work / "depth_zones_eu.fgb"
    if not skip_if_exists(polygons):
        downsampled = work / "emodnet_depth_zones_polygonize.tif"
        if not skip_if_exists(downsampled):
            gdalwarp(
                classified, downsampled,
                resolution=EMODNET_POLYGONIZE_RES,
                resampling="near",
            )

        raw_polygons = work / "depth_zones_eu_raw.fgb"
        gdal_polygonize(downsampled, raw_polygons, layer="depth_zones_eu", field="min_depth")

        # Ocean only (exclude land=1 and nodata=-32768)
        ogr2ogr(raw_polygons, polygons, where="min_depth < 0 AND min_depth > -32768")
        raw_polygons.unlink(missing_ok=True)

    # Step 4b: Clip back to the OSM coastline
    console.print("=== Step 4b: Clip to OSM Coastline ===")
    clipped = work / "depth_zones_eu_clipped.fgb"
    if not skip_if_exists(clipped):
        clip_to_coastline(
            polygons, clipped, require_osm_land(get_work_dir(base)),
            layer="depth_zones_eu", extent=EMODNET_EXTENT, cell_size=1.0,
        )

    # Step 5: PMTiles
    console.print("=== Step 5: Generate PMTiles ===")
    generate_pmtiles(
        clipped, pmtiles,
        layer="depth_zones_eu",
        min_zoom=EMODNET_MIN_ZOOM, max_zoom=EMODNET_MAX_ZOOM,
        coalesce_densest=True,
        extra_args=[
            "--simplification=2",
            "--no-feature-limit",
            "--no-tile-size-limit",
            "--detect-shared-borders",
        ],
    )

    console.print(f"[green]Done:[/green] {pmtiles}")
