"""Pipeline step 05d: Natural Earth boundary lines → PMTiles.

The viewer used to take its boundaries from Esri's reference raster, which
bakes country and internal boundaries into the same pixels: they could not be
told apart, restyled, or switched off, and they came out heavy. These are
vector lines in two layers instead --

    countries        admin-0 land boundaries
    regions          admin-1 state and province lines
    municipalities   admin-2 units: the limits a city is administered within

-- so a federal republic's internal border can be drawn lighter than an
international one, and each can be toggled on its own.

The country polygons in land_base carry boundaries too, but as polygon
outlines: every coast is drawn as a border. These are boundary lines proper,
which stop at the coast.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from basemap.config import (
    BOUNDARY_SHAPEFILES,
    GEOBOUNDARIES_ADM2_URL,
    SHORELINE_DETAIL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.utils.download import download_file, download_shapefile
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()


def municipalities(work: Path) -> Path:
    """Second-level administrative units worldwide, as polygons.

    These are what a city's limits actually are. Built-up areas, which this
    replaces, are land cover: they show where the houses are, which is a
    blotch that stops and starts with the suburbs and belongs to nobody.

    geoBoundaries covers the world at this level. Norway comes from N50
    instead: geoBoundaries' Norwegian units are sourced from Kartverket too,
    but from 2013, and the country has redrawn its municipalities twice since
    -- the mergers of 2020 and the splits of 2024.
    """
    import fiona
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping, shape

    out = work / "boundaries" / "municipalities.fgb"
    if skip_if_exists(out):
        return out
    gpkg = work / "boundaries" / "geoBoundariesCGAZ_ADM2.gpkg"
    if not gpkg.is_file():
        download_file(GEOBOUNDARIES_ADM2_URL, gpkg, label="geoBoundaries ADM2 (~230 MB)")

    schema = {"geometry": "MultiPolygon",
              "properties": {"name": "str", "country": "str"}}
    world = norway = 0
    with fiona.open(out, "w", driver="FlatGeobuf", crs="EPSG:4326",
                    schema=schema, layer="municipalities") as dst:
        with fiona.open(gpkg) as src:
            for f in src:
                props = f["properties"]
                if props.get("shapeGroup") == "NOR":
                    continue
                geom = f["geometry"]
                dst.write({
                    "type": "Feature",
                    "geometry": geom if geom["type"] == "MultiPolygon" else {
                        "type": "MultiPolygon", "coordinates": [geom["coordinates"]]},
                    "properties": {"name": props.get("shapeName"),
                                   "country": props.get("shapeGroup")},
                })
                world += 1
        from basemap.pipeline.lakes import n50_gdb

        with fiona.open(n50_gdb(work), layer="N50_AdministrativeOmråder_omrade") as src:
            for f in src:
                props = f["properties"]
                geom = transform_geom(src.crs, "EPSG:4326", mapping(shape(f["geometry"])))
                dst.write({
                    "type": "Feature",
                    "geometry": geom if geom["type"] == "MultiPolygon" else {
                        "type": "MultiPolygon", "coordinates": [geom["coordinates"]]},
                    "properties": {"name": props.get("kommunenavn"), "country": "NOR"},
                })
                norway += 1
    console.print(f"  {world:,} units worldwide, {norway} Norwegian kommuner")
    return out


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05d: Generate Boundary Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = ensure_dir(get_work_dir(base) / "boundaries")
    tile_dir = ensure_dir(get_tile_dir(base))

    console.print("=== Step 1: Natural Earth boundary lines ===")
    inputs = []
    for layer, (name, url, zooms) in BOUNDARY_SHAPEFILES.items():
        shp = download_shapefile(name, url, work)
        geojson = work / f"{layer}.geojson"
        if not skip_if_exists(geojson):
            ogr2ogr(shp, geojson, output_format="GeoJSON")
        inputs.append({"file": geojson, "layer": layer, "minzoom": zooms[0], "maxzoom": zooms[1]})

    console.print("=== Step 1b: Municipal boundaries ===")
    # Stopping at z10 and letting the viewer overzoom. Kept to z12 at full
    # detail, 49,000 administrative polygons made a 1 GB archive against the
    # 36 MB the country and region lines take, and the z11 and z12 tiles are
    # nearly all of that -- for a dashed line whose shape is already settled.
    # Vector geometry overzooms cleanly; only the simplification tolerance
    # shows, and at this scale a city limit is a smooth curve anyway.
    inputs.append({
        "file": municipalities(ensure_dir(work)), "layer": "municipalities",
        "minzoom": 6, "maxzoom": 10,
    })

    console.print("=== Step 2: PMTiles ===")
    pmtiles = tile_dir / "boundaries.pmtiles"
    if not skip_if_exists(pmtiles):
        generate_pmtiles_multilayer(
            inputs, pmtiles,
            min_zoom=min(i["minzoom"] for i in inputs),
            max_zoom=max(i["maxzoom"] for i in inputs),
            detail=SHORELINE_DETAIL,
            extra_args=[
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--simplification=4",
            ],
        )

    console.print(f"[green]Done:[/green] {pmtiles}")
