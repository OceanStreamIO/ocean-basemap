"""Pipeline step 04: Generate ocean labels → PMTiles."""

from __future__ import annotations

from pathlib import Path

import httpx
from rich.console import Console

from basemap.config import NE_GEOJSON_BASE, SCUFN_URL, get_data_dir, get_tile_dir, get_work_dir
from basemap.processing.labels import merge_ocean_labels
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles

console = Console()


def _try_scufn(dest: Path, timeout: float = 120) -> bool:
    """Attempt to download SCUFN gazetteer from NOAA ArcGIS REST. Returns success."""
    if dest.is_file():
        console.print("  [dim]SCUFN cached, skipping download.[/dim]")
        return True

    console.print("  Attempting SCUFN Gazetteer download...")
    try:
        # Quick connectivity test
        httpx.get(
            SCUFN_URL,
            params={
                "where": "1=1", "outFields": "Name,Generic",
                "f": "geojson", "resultRecordCount": "1",
            },
            timeout=10,
        )
    except (httpx.TimeoutException, httpx.ConnectError):
        console.print("  [yellow]NOAA server unreachable, skipping SCUFN.[/yellow]")
        return False

    try:
        r = httpx.get(
            SCUFN_URL,
            params={
                "where": "1=1",
                "outFields": "Name,Generic",
                "f": "geojson",
                "resultRecordCount": "10000",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        count = len(data.get("features", []))
        if count > 100:
            dest.write_text(r.text)
            console.print(f"  [green]SCUFN[/green]: {count} features downloaded.")
            return True
        console.print(f"  [yellow]SCUFN returned only {count} features, skipping.[/yellow]")
    except Exception as e:
        console.print(f"  [yellow]SCUFN download failed: {e}[/yellow]")
    return False


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 04: Generate Ocean Labels[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base) / "labels"
    tile_dir = get_tile_dir(base)
    ensure_dir(work)
    ensure_dir(tile_dir)

    # Step 1: Download Natural Earth data
    console.print("=== Step 1: Download Natural Earth Data ===")
    marine_polys = work / "ne_10m_geography_marine_polys.geojson"
    if not skip_if_exists(marine_polys):
        download_file(
            f"{NE_GEOJSON_BASE}/ne_10m_geography_marine_polys.geojson",
            marine_polys,
        )

    region_points = work / "ne_10m_geography_regions_points.geojson"
    if not skip_if_exists(region_points):
        download_file(
            f"{NE_GEOJSON_BASE}/ne_10m_geography_regions_points.geojson",
            region_points,
        )

    # Step 2: SCUFN (optional)
    console.print("=== Step 2: SCUFN Gazetteer (optional) ===")
    scufn_path = work / "scufn_features.geojson"
    scufn_ok = _try_scufn(scufn_path)

    # Step 3: Process & merge
    console.print("=== Step 3: Process & Merge Labels ===")
    merged = work / "ocean_labels_merged.geojson"
    if not skip_if_exists(merged):
        merge_ocean_labels(
            marine_polys, region_points,
            scufn_path if scufn_ok else None,
            merged,
        )

    # Step 4: PMTiles
    console.print("=== Step 4: Generate PMTiles ===")
    pmtiles = tile_dir / "ocean_labels.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles(
            merged, pmtiles,
            layer="ocean_labels",
            min_zoom=1, max_zoom=10,
            extra_args=["--no-feature-limit", "--no-tile-size-limit"],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
