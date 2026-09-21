"""Pipeline step 05e: place names.

Labels belong on top of the rendered relief as vectors, not baked into it:
they have to stay upright as the map turns, grow and shrink with the zoom,
step aside from each other, and be capable of being switched off or restyled.
Pixels can do none of that, which is the same reason Esri's raster labels
cannot have their boundaries softened.

Cities were here too, briefly, as polygons -- built-up areas from land cover
and urban centres from the Global Human Settlement Layer. Both were too coarse
to be worth drawing: land cover is a blotch of houses and the urban centres are
a 1 km grid smoothed, which reads as a lump rather than a city. Anyone who
needs a city's shape has one better than either, and can put it on the map with
``?overlay=``. What stays here is the names.

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
    GEONAMES_URL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.pipeline.lakes import n50_gdb
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()

N50_NAMES_LAYER = "N50_Stedsnavn_tekstplassering"

# Kartverket's own type sizes, and the zoom range each earns: cities from z6,
# towns from z8, villages from z10, everything down to the farms once close.
#
# Each band holds every name at or above its threshold, not only the names of
# its own class, and the ranges do not overlap. Both halves matter. Written the
# other way -- each band its own class, all of them running to the deepest zoom
# -- the bands collide on every tile they share, tile-join keeps one of them,
# and the rest of the names are never drawn at all. Faro had four places in it
# where Portugal has dozens.
NAME_BANDS: tuple[tuple[float, tuple[int, int]], ...] = (
    (12.0, (6, 7)),
    (10.0, (8, 9)),
    (8.5, (10, 10)),
    (7.5, (11, 11)),
    (0.0, (12, 14)),
)


# GeoNames ranks by population, which plays the part type size plays in N50.
# A capital is always drawn, however small the country. Cumulative and
# non-overlapping, for the reason given at NAME_BANDS. The last
# band runs to 14, the depth of the archive: a layer that stops earlier is
# absent from the deeper tiles rather than overzoomed, and the world's names
# would vanish at z13 while Norway's carried on.
WORLD_BANDS: tuple[tuple[int, tuple[int, int]], ...] = (
    (1_000_000, (3, 4)),
    (200_000, (5, 6)),
    (50_000, (7, 8)),
    (10_000, (9, 10)),
    (1_000, (11, 11)),
    (0, (12, 14)),
)
# Tippecanoe thins points as it goes down the zooms -- its drop rate defaults
# to dropping a share of them per level, which is what a map wants when every
# point is equal. These are not: each band already says the zoom its places
# have earned, by population or by Kartverket's type size, so the thinning has
# already been done and tippecanoe doing it again leaves eleven names across
# the whole of Europe. "-r1" turns it off.
KEEP_EVERY_POINT = ["-r1", "--no-feature-limit", "--no-tile-size-limit"]

# The columns of the GeoNames dump that matter (see its readme).
GN_NAME, GN_LAT, GN_LON, GN_CODE, GN_COUNTRY, GN_POP = 1, 4, 5, 7, 8, 14


def world_bands(work: Path) -> list[dict]:
    """GeoNames' populated places, split into bands by population.

    Norway is left out: N50 names it far better, down to the farms, and two
    sources over the same ground would print each town twice.
    """
    import csv
    import io
    import zipfile

    out_dir = ensure_dir(work / "places")
    paths = [out_dir / f"world_{i}.geojson" for i in range(len(WORLD_BANDS))]
    made = [
        {"file": p, "layer": "world", "minzoom": z[0], "maxzoom": z[1]}
        for p, (_, z) in zip(paths, WORLD_BANDS, strict=True)
    ]
    if all(skip_if_exists(p) for p in paths):
        return made

    archive = out_dir / "cities500.zip"
    if not archive.is_file():
        download_file(GEONAMES_URL, archive, label="GeoNames cities500")
    banded: list[list[dict]] = [[] for _ in WORLD_BANDS]
    with zipfile.ZipFile(archive) as zf:
        raw = zf.read("cities500.txt").decode("utf-8")
    for row in csv.reader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) <= GN_POP or row[GN_COUNTRY] == "NO":
            continue
        try:
            lon, lat = float(row[GN_LON]), float(row[GN_LAT])
            population = int(row[GN_POP] or 0)
        except ValueError:
            continue
        capital = row[GN_CODE] == "PPLC"
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": row[GN_NAME],
                "kind": row[GN_CODE],
                "population": population,
                "capital": capital,
            },
        }
        # Into its own band and every deeper one, so a city still appears on
        # the zooms below the one it first earns.
        for i, (threshold, _) in enumerate(WORLD_BANDS):
            if capital or population >= threshold:
                for band in banded[i:]:
                    band.append(feature)
                break
    for path, features, (threshold, zooms) in zip(paths, banded, WORLD_BANDS, strict=True):
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        console.print(
            f"  {len(features):7,} places with population >= {threshold:,}"
            f"  z{zooms[0]}-{zooms[1]}"
        )
    return made


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
            point = shape(f["geometry"]).centroid
            size = float(props.get("FontSize") or 0.0)
            feature = {
                "type": "Feature",
                "geometry": transform_geom(src.crs, "EPSG:4326", mapping(point)),
                "properties": {
                    "name": name,
                    "kind": props.get("navneobjekttype"),
                    "rank": size,
                },
            }
            # Into its own band and every deeper one, so a city is still named
            # on the zooms past the one where it first appears.
            for i, (threshold, _) in enumerate(NAME_BANDS):
                if size >= threshold:
                    for band in banded[i:]:
                        band.append(feature)
                    break

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

    console.print("=== Place names ===")
    places = tile_dir / "places.pmtiles"
    if not skip_if_exists(places):
        generate_pmtiles_multilayer(
            world_bands(work) + name_bands(work),
            places, min_zoom=3, max_zoom=14,
            extra_args=KEEP_EVERY_POINT,
        )

    console.print(f"[green]Done:[/green] {places}")
