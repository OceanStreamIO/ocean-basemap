"""Pipeline step 05: Natural Earth + OSM → PMTiles for land/coastline/cities."""

from __future__ import annotations

import zipfile
from pathlib import Path

from rich.console import Console

from basemap.config import NE_SHAPEFILES, OSM_URLS, get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.download import download_file
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()


def _download_and_extract_ne(name: str, url: str, ne_dir: Path) -> Path:
    """Download a Natural Earth zip and extract the shapefile."""
    shp = ne_dir / f"{name}.shp"
    if shp.exists():
        console.print(f"  [dim]{name} already extracted.[/dim]")
        return shp

    zip_path = ne_dir / f"{name}.zip"
    download_file(url, zip_path, label=name)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(ne_dir)
    return shp


def _convert_to_geojson(shp: Path, out: Path) -> Path:
    if not skip_if_exists(out):
        ogr2ogr(shp, out, output_format="GeoJSON")
    return out


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05: Generate Land Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base) / "land"
    tile_dir = get_tile_dir(base)
    ne_dir = work / "natural_earth"
    osm_dir = work / "osm"
    ensure_dir(ne_dir)
    ensure_dir(osm_dir)
    ensure_dir(tile_dir)

    # Step 1a: Download Natural Earth
    console.print("=== Step 1a: Download Natural Earth ===")
    ne_shps: dict[str, Path] = {}
    for name, url in NE_SHAPEFILES.items():
        ne_shps[name] = _download_and_extract_ne(name, url, ne_dir)

    # Step 1b: Download OSM coastline data
    console.print("=== Step 1b: Download OSM Coastline Data ===")
    osm_land_shp = osm_dir / "land-polygons-split-4326" / "land_polygons.shp"
    if not osm_land_shp.exists():
        zip_path = osm_dir / "land-polygons-split-4326.zip"
        download_file(OSM_URLS["land"], zip_path, label="OSM land polygons (~620 MB)")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(osm_dir)

    osm_coast_shp = osm_dir / "coastlines-split-4326" / "lines.shp"
    if not osm_coast_shp.exists():
        zip_path = osm_dir / "coastlines-split-4326.zip"
        download_file(OSM_URLS["coastline"], zip_path, label="OSM coastlines (~70 MB)")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(osm_dir)

    # Step 2: Convert to GeoJSON / FlatGeobuf
    console.print("=== Step 2: Convert to GeoJSON ===")
    land_110m = _convert_to_geojson(ne_shps["ne_110m_land"], work / "land_110m.geojson")
    land_10m = _convert_to_geojson(ne_shps["ne_10m_land"], work / "land_10m.geojson")
    coastline_50m = _convert_to_geojson(ne_shps["ne_50m_coastline"], work / "coastline_50m.geojson")
    coastline_10m = _convert_to_geojson(ne_shps["ne_10m_coastline"], work / "coastline_10m.geojson")
    countries_10m = _convert_to_geojson(
        ne_shps["ne_10m_admin_0_countries"], work / "countries_10m.geojson",
    )
    cities_10m = _convert_to_geojson(
        ne_shps["ne_10m_populated_places_simple"], work / "cities_10m.geojson",
    )
    rivers_50m = _convert_to_geojson(
        ne_shps["ne_50m_rivers_lake_centerlines"], work / "rivers_50m.geojson",
    )
    rivers_10m = _convert_to_geojson(
        ne_shps["ne_10m_rivers_lake_centerlines"], work / "rivers_10m.geojson",
    )
    lakes_110m = _convert_to_geojson(ne_shps["ne_110m_lakes"], work / "lakes_110m.geojson")
    lakes_50m = _convert_to_geojson(ne_shps["ne_50m_lakes"], work / "lakes_50m.geojson")
    lakes_10m = _convert_to_geojson(ne_shps["ne_10m_lakes"], work / "lakes_10m.geojson")

    # Large OSM → FlatGeobuf (faster for tippecanoe)
    osm_land_fgb = work / "osm_land.fgb"
    if not skip_if_exists(osm_land_fgb):
        ogr2ogr(osm_land_shp, osm_land_fgb, output_format="FlatGeobuf")

    osm_coast_fgb = work / "osm_coastline.fgb"
    if not skip_if_exists(osm_coast_fgb):
        ogr2ogr(osm_coast_shp, osm_coast_fgb, output_format="FlatGeobuf")

    # Step 3: PMTiles (multi-resolution strategy)
    console.print("=== Step 3: Generate PMTiles ===")
    pmtiles = tile_dir / "land_base.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles_multilayer(
            [
                {"file": land_110m, "layer": "land", "minzoom": 0, "maxzoom": 2},
                {"file": land_10m, "layer": "land", "minzoom": 3, "maxzoom": 4},
                {"file": osm_land_fgb, "layer": "land", "minzoom": 5},
                {"file": coastline_50m, "layer": "coastline", "minzoom": 0, "maxzoom": 2},
                {"file": coastline_10m, "layer": "coastline", "minzoom": 3, "maxzoom": 4},
                {"file": osm_coast_fgb, "layer": "coastline", "minzoom": 5},
                {"file": lakes_110m, "layer": "lakes", "minzoom": 0, "maxzoom": 2},
                {"file": lakes_50m, "layer": "lakes", "minzoom": 3, "maxzoom": 4},
                {"file": lakes_10m, "layer": "lakes", "minzoom": 5},
                {"file": rivers_50m, "layer": "rivers", "minzoom": 3, "maxzoom": 4},
                {"file": rivers_10m, "layer": "rivers", "minzoom": 5},
                {"file": countries_10m, "layer": "countries", "minzoom": 2},
                {"file": cities_10m, "layer": "cities", "minzoom": 2},
            ],
            pmtiles,
            min_zoom=0, max_zoom=10,
            extra_args=[
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--detect-shared-borders",
                "--no-simplification-of-shared-nodes",
                "--no-line-simplification",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
