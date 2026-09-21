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
    GEONAMES_URL,
    GHS_UCDB_URL,
    NE_URBAN_URL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.pipeline.lakes import N50_LAND_COVER, n50_gdb
from basemap.utils.download import download_file, download_shapefile
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()

N50_NAMES_LAYER = "N50_Stedsnavn_tekstplassering"
N50_BUILT_UP = ("Tettbebyggelse", "BymessigBebyggelse")

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
# A capital is always drawn, however small the country.
# Cumulative and non-overlapping, for the reason given at NAME_BANDS. The last
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


def city_areas(work: Path) -> Path:
    """Cities as polygons, with their names and populations.

    Three different things get called a city's shape. Land cover is where the
    houses reach, which starts and stops with the suburbs. An administrative
    unit is what the city is governed within, which in Norway is a kommune
    reaching far past the town and in Germany may be a whole Land. This is the
    third: the urban centre itself, as the Global Human Settlement Layer
    delineates it from built-up density and population -- Oslo as one shape of
    242 square kilometres.

    Being polygons is also what puts the name on the map at close range. A
    point label has one anchor, and once the zoom passes the point where that
    anchor leaves the screen the city's name goes with it; MapLibre gives a
    polygon a label in every tile it covers, so the name stays while you are
    inside the city.
    """
    import zipfile

    out = work / "places" / "cities.fgb"
    if skip_if_exists(out):
        return out
    places = ensure_dir(work / "places")
    archive = places / "ghs_ucdb.zip"
    if not archive.is_file():
        download_file(GHS_UCDB_URL, archive, label="GHS urban centres (~42 MB)")
    folder = places / "ghs_ucdb"
    gpkg = next(folder.rglob("*_V1_2.gpkg"), None) if folder.is_dir() else None
    if gpkg is None:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(folder)
        gpkg = next(folder.rglob("*_V1_2.gpkg"))

    schema = {"geometry": "MultiPolygon",
              "properties": {"name": "str", "country": "str", "population": "float"}}
    kept = 0
    with fiona.open(gpkg) as src, fiona.open(
        out, "w", driver="FlatGeobuf", crs="EPSG:4326", schema=schema, layer="cities"
    ) as dst:
        for f in src:
            props = f["properties"]
            name = (props.get("UC_NM_MN") or "").strip()
            # The database marks an unnamed centre with N/A rather than a blank.
            if not name or name.upper() in {"N/A", "NA"}:
                continue
            geom = f["geometry"]
            dst.write({
                "type": "Feature",
                "geometry": geom if geom["type"] == "MultiPolygon" else {
                    "type": "MultiPolygon", "coordinates": [geom["coordinates"]]},
                "properties": {
                    "name": name,
                    "country": props.get("CTR_MN_NM"),
                    "population": float(props.get("P15") or 0.0),
                },
            })
            kept += 1
    console.print(f"  {kept:,} named urban centres")
    return out


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


def name_bands(work: Path, cities: Path | None = None) -> list[dict]:
    """N50's place names as points, split into bands by the type size.

    The annotation carries the whole name in ``fulltekst``; ``TextString`` is
    only the line it is broken across, so a hyphenated name would arrive in
    pieces. Its geometry is the box the text occupies rather than the place, so
    the point taken here is that box's centre, which is where the label sits on
    Kartverket's own map. Names repeat across map sheets, so ``stedsnummer``
    keeps each place once.

    A name is dropped when a city polygon of the same name already covers it:
    the polygon carries its own label, so Oslo was printed twice, once across
    the city and once wherever Kartverket had put the word -- which for Oslo is
    out in the fjord.

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

    import shapely

    named_cities: list = []
    city_names: list[str] = []
    if cities is not None and cities.is_file():
        with fiona.open(cities) as src:
            for f in src:
                named_cities.append(shape(f["geometry"]))
                city_names.append((f["properties"].get("name") or "").casefold())
    city_tree = shapely.STRtree(named_cities) if named_cities else None

    banded: list[list[dict]] = [[] for _ in NAME_BANDS]
    seen: set = set()
    dropped = 0
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
            if city_tree is not None:
                lonlat = shape(transform_geom(src.crs, "EPSG:4326", mapping(point)))
                if any(
                    city_names[i] == name.casefold()
                    for i in city_tree.query(lonlat, predicate="within")
                ):
                    dropped += 1
                    continue
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

    if dropped:
        console.print(f"  {dropped} names dropped: a city polygon already carries them")
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
                # To the archive's own maximum, not less. Layers in one
                # archive that stop early do not overzoom -- the deeper tiles
                # simply do not contain them, so the cities vanished at z13
                # while the Norwegian built-up areas beside them carried on.
                {"file": city_areas(work), "layer": "cities",
                 "minzoom": 4, "maxzoom": 14},
            ],
            urban, min_zoom=4, max_zoom=14,
            extra_args=["--no-feature-limit", "--no-tile-size-limit"],
        )

    console.print("=== Step 2: Place names ===")
    places = tile_dir / "places.pmtiles"
    if not skip_if_exists(places):
        generate_pmtiles_multilayer(
            world_bands(work) + name_bands(work, city_areas(work)),
            places, min_zoom=3, max_zoom=14,
            extra_args=KEEP_EVERY_POINT,
        )

    console.print(f"[green]Done:[/green] {urban}, {places}")
