"""Pipeline step 05e: where people are — built-up areas and place names.

Both belong on top of the rendered relief as vectors, not baked into it.
Labels have to stay upright as the map turns, grow and shrink with the zoom,
step aside from each other, and be capable of being switched off or restyled;
pixels can do none of that, which is the same reason Esri's raster labels
cannot have their boundaries softened. Built-up areas could be baked, but they
are drawn from the same sources as the names and change on the same cadence,
so they are kept beside them.

    urban.pmtiles      built-up areas
        urban          Natural Earth urban areas, worldwide      z4-9
        urban_detail   N50 built-up areas, Norway                z10-14

    places.pmtiles     place names
        places         N50 place names, Norway                   z6-14

Natural Earth's populated places, which the land tiles already carry, name
7,342 settlements worldwide -- two of them around the whole Oslofjord. It is a
1:10M product and was never meant to name a village. Kartverket's N50 has
113,781 settlement names for Norway alone, and ranks them the way its own maps
do, by the size of type each is drawn in: 12.7 for a city, down to 6.0 for a
single farm. That ranking is what decides the zoom a name appears at here, so
the map thins out the way a paper map does rather than by a rule invented for
it.
"""

from __future__ import annotations

import json
from pathlib import Path

import fiona
from rasterio.warp import transform_geom
from rich.console import Console
from rich.progress import Progress
from shapely.geometry import mapping, shape

from basemap.config import (
    NE_URBAN_URL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.pipeline.lakes import N50_LAND_COVER, n50_gdb
from basemap.utils.download import download_shapefile
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()

N50_NAMES_LAYER = "N50_Stedsnavn_tekstplassering"
N50_BUILT_UP = ("Tettbebyggelse", "BymessigBebyggelse")

# Kartverket's own type sizes, and the zoom each band earns. A city is drawn at
# 12.7 and a farm at 6.0, so this reads as: cities from z6, towns from z8,
# villages from z10, and everything down to the farms once you are close.
NAME_BANDS: tuple[tuple[float, tuple[int, int]], ...] = (
    (12.0, (6, 14)),
    (10.0, (8, 14)),
    (8.5, (10, 14)),
    (7.5, (11, 14)),
    (0.0, (12, 14)),
)


def urban_world(work: Path) -> Path:
    """Natural Earth's urban areas, as GeoJSON."""
    out = work / "places" / "urban_world.geojson"
    if skip_if_exists(out):
        return out
    shp = download_shapefile("ne_10m_urban_areas", NE_URBAN_URL, ensure_dir(work / "places"))
    return ogr2ogr(shp, out, output_format="GeoJSON")


def urban_norway(work: Path) -> Path:
    """N50's built-up areas, in longitude/latitude.

    Industrial estates are left out: they are built-up in the land-cover sense
    but not somewhere anyone lives, and at these zooms they read as a town.
    """
    out = work / "places" / "urban_norway.fgb"
    if skip_if_exists(out):
        return out
    types = ", ".join(f"'{t}'" for t in N50_BUILT_UP)
    return ogr2ogr(
        n50_gdb(work), out, output_format="FlatGeobuf", nln="urban_detail", t_srs="EPSG:4326",
        sql=f"SELECT objtype FROM {N50_LAND_COVER} WHERE objtype IN ({types})",
    )


def name_bands(work: Path) -> list[dict]:
    """N50's place names as points, split into bands by the type size.

    The annotation carries the whole name in ``fulltekst``; ``TextString`` is
    only the line it is broken across, so a hyphenated name would arrive in
    pieces. Its geometry is the box the text occupies rather than the place, so
    the point taken here is that box's centre, which is where the label sits on
    Kartverket's own map. Names repeat across map sheets, so ``stedsnummer``
    keeps each place once.

    The centres are reprojected here rather than by ogr2ogr, because they are
    derived rather than read: the geodatabase is UTM 33N, so a point left in it
    is metres from a false origin, which tippecanoe drops as an impossible
    longitude without saying anything. The symptom is an archive of no size.
    """
    out_dir = ensure_dir(work / "places")
    paths = [out_dir / f"names_{i}.geojson" for i in range(len(NAME_BANDS))]
    if all(skip_if_exists(p) for p in paths):
        return [
            {"file": p, "layer": "places", "minzoom": z[0], "maxzoom": z[1]}
            for p, (_, z) in zip(paths, NAME_BANDS, strict=True)
        ]

    banded: list[list[dict]] = [[] for _ in NAME_BANDS]
    seen: set = set()
    gdb = n50_gdb(work)
    with fiona.open(gdb, layer=N50_NAMES_LAYER) as src, Progress(
        console=console, transient=True
    ) as progress:
        task = progress.add_task("N50 place names", total=len(src))
        for f in src:
            progress.advance(task)
            props = f["properties"]
            if props.get("navneobjekthovedgruppe") != "bebyggelse":
                continue
            name = (props.get("fulltekst") or "").strip()
            key = props.get("stedsnummer")
            if not name or (key is not None and key in seen):
                continue
            if key is not None:
                seen.add(key)
            size = float(props.get("FontSize") or 0.0)
            for i, (threshold, _) in enumerate(NAME_BANDS):
                if size >= threshold:
                    break
            banded[i].append({
                "type": "Feature",
                "geometry": transform_geom(
                    src.crs, "EPSG:4326", mapping(shape(f["geometry"]).centroid)
                ),
                "properties": {
                    "name": name,
                    "kind": props.get("navneobjekttype"),
                    "rank": size,
                },
            })

    inputs = []
    for path, features, (threshold, zooms) in zip(paths, banded, NAME_BANDS, strict=True):
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        console.print(
            f"  {len(features):6,} names at type size >= {threshold:4.1f}"
            f"  z{zooms[0]}-{zooms[1]}"
        )
        inputs.append({"file": path, "layer": "places", "minzoom": zooms[0], "maxzoom": zooms[1]})
    return inputs


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05e: Generate Place Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = ensure_dir(get_tile_dir(base))

    console.print("=== Step 1: Built-up areas ===")
    urban = tile_dir / "urban.pmtiles"
    if not skip_if_exists(urban):
        generate_pmtiles_multilayer(
            [
                {"file": urban_world(work), "layer": "urban", "minzoom": 4, "maxzoom": 9},
                {"file": urban_norway(work), "layer": "urban_detail",
                 "minzoom": 10, "maxzoom": 14},
            ],
            urban, min_zoom=4, max_zoom=14,
            extra_args=["--no-feature-limit", "--no-tile-size-limit"],
        )

    console.print("=== Step 2: Place names ===")
    places = tile_dir / "places.pmtiles"
    if not skip_if_exists(places):
        generate_pmtiles_multilayer(
            name_bands(work), places, min_zoom=6, max_zoom=14,
            extra_args=["--no-feature-limit", "--no-tile-size-limit"],
        )

    console.print(f"[green]Done:[/green] {urban}, {places}")
