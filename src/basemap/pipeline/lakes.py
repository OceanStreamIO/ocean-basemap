"""Pipeline step 05c: HydroLAKES → lake polygons as PMTiles.

The lakes were Natural Earth's 1:10M outlines: a few thousand coarse shapes,
drawn in one flat blue. HydroLAKES has 1.4 million lakes with accurate
shorelines, and carries each lake's mean depth and surface elevation, so lakes
can be coloured on the same depth ramp as the sea instead of one flat tone.

Lakes are tiled in bands by area, so a zoomed-out view carries the few hundred
lakes big enough to see rather than every pond on the planet:

    z2-4    >= 1000 km2     178 lakes
    z5-6    >= 100 km2      1,708
    z7-8    >= 10 km2       16,689
    z9-10   >= 1 km2        185,181
    z11-12  all             1,427,688

The depth in these attributes is a modelled mean, one number per lake. Where
GEBCO carries a surveyed lake bed -- Caspian, Baikal, most of the Great Lakes,
Geneva, Constance, Vanern and others -- this step also renders the lakes'
depth as raster tiles, the same way the sea is rendered. GEBCO stores a lake
bed as its height above sea level, so the depth is the lake's surface
elevation minus the bed; HydroLAKES supplies the surface.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from rich.console import Console
from rich.progress import Progress
from shapely.geometry import shape

from basemap.config import (
    COASTAL_LAKE_DEPTH,
    COASTAL_LAKE_ELEV,
    HYDROLAKES_URL,
    LAKE_BANDS,
    LAKE_RELIEF_AREA,
    LAKE_RELIEF_DEPTH,
    LAKE_RELIEF_ZOOMS,
    LAKE_SOURCES_FILE,
    N50_URL,
    RAMP_DIR,
    SHORELINE_DETAIL,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.pipeline.bathymetry import gebco_source
from basemap.processing.raster_tiles import (
    Band,
    Datum,
    Ramp,
    RenderSpec,
    Shading,
    Source,
    finalize,
    render_into,
    stage_path,
    tiles_in_bbox,
)
from basemap.utils.download import download_file
from basemap.utils.gdal import ogr2ogr
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import generate_pmtiles_multilayer

console = Console()

# Where N50 applies. Norway plus a margin, so the HydroLAKES index built for
# the overlap test covers every lake N50 might duplicate.
N50_EXTENT = (4.0, 57.5, 32.0, 71.5)
N50_LAND_COVER = "N50_Arealdekke_omrade"
N50_LAKE_TYPES = ("Innsjø", "InnsjøRegulert")

# The attributes worth carrying into the tiles, renamed to the style's casing.
FIELDS = (
    "Lake_name AS name",
    "Lake_area AS area_km2",
    "Depth_avg AS depth_avg",
    "Elevation AS elev_m",
)

# What the N50 lakes are written as. It has to match the bands they are
# appended to, which ogr2ogr writes as Polygon with these four fields: a
# FlatGeobuf will not take a geometry or a field its schema does not declare.
# N50 stores each lake as a MultiPolygon, so a multi-part one is split -- its
# parts are separate bodies of water anyway, and the rings that make islands
# stay inside the part that holds them.
N50_SCHEMA = {
    "geometry": "Polygon",
    "properties": {
        "name": "str", "area_km2": "float", "depth_avg": "float", "elev_m": "float",
    },
}


def source_shapefile(work: Path) -> Path:
    """Download and unpack HydroLAKES, returning its shapefile."""
    lakes = ensure_dir(work / "lakes")
    shp = lakes / "HydroLAKES_polys_v10_shp" / "HydroLAKES_polys_v10.shp"
    if shp.is_file():
        console.print("  [dim]HydroLAKES already extracted.[/dim]")
        return shp
    archive = lakes / "HydroLAKES_polys_v10_shp.zip"
    download_file(HYDROLAKES_URL, archive, label="HydroLAKES (~820 MB)")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(lakes)
    return shp


def n50_gdb(work: Path) -> Path:
    """Download and unpack Kartverket's N50, returning the file geodatabase."""
    n50 = ensure_dir(work / "n50")
    gdb = n50 / "Basisdata_0000_Norge_25833_N50Kartdata_FGDB.gdb"
    if gdb.is_dir():
        console.print("  [dim]N50 already extracted.[/dim]")
        return gdb
    archive = n50 / "Basisdata_0000_Norge_25833_N50Kartdata_FGDB.zip"
    download_file(N50_URL, archive, label="N50 Kartdata (~3.9 GB)")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(n50)
    return gdb


def _KM2_PER_DEG2(geom):
    """Rough square kilometres per square degree at this latitude."""
    import math

    lat = geom.representative_point().y
    return 111.32 * 111.32 * math.cos(math.radians(lat))


def _containing(geom, tree):
    """Index of the HydroLAKES lake holding this N50 lake, or None.

    shapely's STRtree applies a predicate with the *query* geometry first, so
    ``query(point, predicate="within")`` asks whether the point lies within
    each candidate. Asked as ``"contains"`` it reads the other way and never
    matches, which is how a million Norwegian lakes once reported no overlap
    with HydroLAKES at all.
    """
    if tree is None:
        return None
    hit = tree.query(geom.representative_point(), predicate="within")
    return int(hit[0]) if len(hit) else None


def n50_lakes(work: Path, shp: Path) -> tuple[Path, Path]:
    """Norway's lakes from N50, carrying HydroLAKES depth where they match.

    N50's outlines are the reason to do this at all. HydroLAKES is generalised
    -- Semsvannet near Asker is 22 vertices there and 165 in N50, for the same
    0.76 square kilometres -- so a lake drawn from HydroLAKES reads as a
    polygon rather than a lake however far in you zoom. Keeping HydroLAKES
    wherever it already had something meant every lake big enough to notice
    kept the coarse outline, and only the small ones Norway was missing came
    out sharp.

    So the geometry is always N50's. What HydroLAKES has and N50 does not is a
    modelled mean depth, which is copied onto the N50 lakes that replace it.

    A HydroLAKES lake is replaced when the N50 lakes inside it account for at
    least 60% of its area -- all of them together, not the largest. Its coarse
    outline often merges several lakes into one blob, and judging each N50 lake
    on its own left 654 of those blobs drawn over the sharp outlines beneath.
    Weighing them together also keeps a lake that N50 only has part of, because
    it crosses the border, from being replaced by that part.
    """
    import shapely

    out = work / "lakes" / "lakes_n50.fgb"
    superseded = work / "lakes" / "lakes_n50_superseded.fgb"
    if skip_if_exists(out) and superseded.is_file():
        return out, superseded
    gdb = n50_gdb(work)

    # Lakes only, already in longitude/latitude, in one GDAL pass.
    raw = work / "lakes" / "lakes_n50_raw.fgb"
    if not skip_if_exists(raw):
        types = ", ".join(f"'{t}'" for t in N50_LAKE_TYPES)
        ogr2ogr(
            gdb, raw, output_format="FlatGeobuf", nln="lakes", t_srs="EPSG:4326",
            sql=(f"SELECT hoyde, SHAPE_Area FROM {N50_LAND_COVER} "
                 f"WHERE objtype IN ({types})"),
        )

    with fiona.open(shp) as src:
        lakes = [(shape(f["geometry"]), f["properties"]) for f in src.filter(bbox=N50_EXTENT)]
    tree = shapely.STRtree([g for g, _ in lakes]) if lakes else None
    console.print(f"  {len(lakes)} HydroLAKES lakes over Norway")

    # First pass: which HydroLAKES lake does each N50 lake sit in, and how much
    # of that lake do its occupants add up to?
    owner: list[int | None] = []
    covered: dict[int, float] = {}
    with fiona.open(raw) as src, Progress(console=console, transient=True) as progress:
        task = progress.add_task("matching", total=len(src))
        for f in src:
            progress.advance(task)
            g = shape(f["geometry"])
            i = _containing(g, tree)
            owner.append(i)
            if i is not None:
                covered[i] = covered.get(i, 0.0) + g.area * _KM2_PER_DEG2(g)
    replaced = {
        i for i, area in covered.items()
        if (lakes[i][1].get("Lake_area") or 0.0) and area >= 0.6 * lakes[i][1]["Lake_area"]
    }
    console.print(f"  {len(replaced)} HydroLAKES outlines replaced by sharper N50 ones")

    kept = 0
    with fiona.open(raw) as src, fiona.open(
        out, "w", driver="FlatGeobuf", crs="EPSG:4326", schema=N50_SCHEMA, layer="lakes"
    ) as dst, Progress(console=console, transient=True) as progress:
        task = progress.add_task("N50 lakes", total=len(src))
        for n, f in enumerate(src):
            progress.advance(task)
            g = shape(f["geometry"])
            props = f["properties"]
            i = owner[n]
            twin = lakes[i][1] if i is not None and i in replaced else None
            elev = props.get("hoyde")
            if elev is None and twin is not None:
                elev = twin.get("Elevation")
            total = props.get("SHAPE_Area")
            geom = f["geometry"]
            parts = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                     else [geom["coordinates"]])
            for rings in parts:
                part = shape({"type": "Polygon", "coordinates": rings})
                # Split the recorded area between the parts by their share of
                # it, so a two-part lake is not counted twice in the bands.
                area_km2 = ((total * part.area / g.area if total and g.area else part.area)
                            / 1e6)
                dst.write({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": rings},
                    "properties": {
                        "name": (twin or {}).get("Lake_name") or None,
                        "area_km2": area_km2,
                        # N50 has no depth of its own. Where a HydroLAKES lake
                        # is the same water, its modelled mean comes across;
                        # otherwise the viewer reads the missing value as zero
                        # and draws the shallowest tone, which is honest.
                        "depth_avg": (twin or {}).get("Depth_avg"),
                        "elev_m": float(elev) if elev is not None else None,
                    },
                })
                kept += 1
    # The outlines the bands must drop: the HydroLAKES lakes themselves.
    with fiona.open(
        superseded, "w", driver="FlatGeobuf", crs="EPSG:4326",
        schema={"geometry": "Polygon", "properties": {}}, layer="superseded"
    ) as sup:
        for i in sorted(replaced):
            g = lakes[i][0]
            for poly in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                sup.write({"type": "Feature", "properties": {},
                           "geometry": poly.__geo_interface__})
    console.print(f"  [green]{kept}[/green] N50 lakes written")
    return out, superseded


def band_file(
    shp: Path, work: Path, min_area: float,
    extra: Path | None = None, superseded: Path | None = None,
) -> Path:
    """The lakes at least *min_area* km2 across, as FlatGeobuf.

    *extra* is a second source in the same schema -- the N50 lakes -- whose
    features are appended after the same area filter, so Norway's small lakes
    appear in the bands they belong to rather than all at once.

    *superseded* holds the N50 outlines that replace a HydroLAKES lake. Any
    HydroLAKES lake sitting under one of those is dropped, so the water is
    drawn once, from the better outline, with the depth carried across.
    """
    name = "all" if min_area <= 0 else f"{min_area:g}"
    out = work / "lakes" / f"lakes_{name}.fgb"
    if not skip_if_exists(out):
        where = "" if min_area <= 0 else f" WHERE Lake_area >= {min_area}"
        ogr2ogr(
            shp, out, output_format="FlatGeobuf", nln="lakes",
            sql=f"SELECT {', '.join(FIELDS)} FROM HydroLAKES_polys_v10{where}",
        )
        if extra is not None:
            added, dropped = _append_band(out, extra, min_area, superseded)
            console.print(
                f"    + {added} N50 lakes >= {min_area:g} km2, "
                f"{dropped} HydroLAKES outlines replaced"
            )
    return out


def _append_band(
    band: Path, extra: Path, min_area: float, superseded: Path | None = None
) -> tuple[int, int]:
    """Copy *extra*'s lakes above the threshold into *band*, in place.

    FlatGeobuf is written once and cannot be appended to, so the band is
    rewritten beside itself and moved over the original only when complete --
    an interrupted run then leaves the original intact rather than a file with
    half the lakes in it, which would look finished.
    """
    import shapely

    tree = None
    if superseded is not None and superseded.is_file():
        with fiona.open(superseded) as src:
            replaced = [shape(f["geometry"]) for f in src]
        tree = shapely.STRtree(replaced) if replaced else None

    tmp = band.with_suffix(".merging.fgb")
    added = dropped = 0
    with fiona.open(band) as src:
        meta = src.meta
        with fiona.open(tmp, "w", **meta) as dst:
            for f in src:
                if tree is not None:
                    point = shape(f["geometry"]).representative_point()
                    if len(tree.query(point, predicate="within")):
                        dropped += 1
                        continue
                dst.write(f)
            with fiona.open(extra) as more:
                for f in more:
                    if (f["properties"].get("area_km2") or 0.0) >= min_area:
                        dst.write(f)
                        added += 1
    tmp.replace(band)
    return added, dropped


def external_sources(work: Path) -> tuple[list[Source], set[str], set[int]]:
    """Read lake-sources.json: rasters for lakes GEBCO has no soundings for.

    Each raster is normalised once to bed elevation above sea level -- what
    GEBCO stores -- so it drops straight into the priority stack ahead of it
    and the rest of the pipeline need know nothing about it.

    Returns the sources, and the lake names and ids they cover, which are kept
    in the surveyed set whatever GEBCO says about them.
    """
    entries = (
        json.loads(LAKE_SOURCES_FILE.read_text()).get("lakes", [])
        if LAKE_SOURCES_FILE.is_file() else []
    )
    if not entries:
        return [], set(), set()

    raw_dir = ensure_dir(work / "lakes" / "sources")
    ready_dir = ensure_dir(raw_dir / "bed")
    sources: list[Source] = []
    names: set[str] = set()
    ids: set[int] = set()
    for entry in entries:
        raw = raw_dir / entry["file"]
        if not raw.is_file():
            console.print(f"  [yellow]{entry['file']} not found in {raw_dir}, skipping[/yellow]")
            continue
        bed = ready_dir / f"{Path(entry['file']).stem}_bed.tif"
        if not bed.is_file():
            to_bed_elevation(raw, bed, entry)
        with rasterio.open(bed) as ds:
            bounds = tuple(ds.bounds)
        sources.append(Source(str(bed), crs=entry.get("crs"), bounds=bounds))
        if entry.get("name"):
            names.add(entry["name"])
        if entry.get("hylak_id"):
            ids.add(int(entry["hylak_id"]))
        credit = f" -- {entry['credit']}" if entry.get("credit") else ""
        console.print(f"  [green]{entry.get('name') or entry.get('hylak_id')}[/green] "
                      f"from {entry['file']}{credit}")
    return sources, names, ids


def to_bed_elevation(raw: Path, dst: Path, entry: dict) -> Path:
    """Write *raw* as bed elevation above sea level, the way GEBCO stores it."""
    with rasterio.open(raw) as src:
        data = src.read(1, masked=True).astype("float32") * float(entry.get("sign", 1))
        if entry.get("depths"):
            surface = entry.get("surface_m")
            if surface is None:
                raise ValueError(f"{entry['file']}: 'depths' needs 'surface_m'")
            data = float(surface) - data
        profile = dict(src.profile)
        profile.update(dtype="float32", count=1, nodata=float("nan"), compress="deflate")
        if entry.get("crs") and not src.crs:
            profile["crs"] = entry["crs"]
    with rasterio.open(dst, "w", **profile) as out:
        out.write(np.asarray(data.filled(float("nan")), dtype="float32"), 1)
    return dst


def surveyed_lakes(
    band: Path,
    gebco: Path,
    dst: Path,
    keep_names: set[str] | None = None,
    keep_ids: set[int] | None = None,
) -> Path:
    """Keep the lakes whose bed GEBCO actually holds, with their surface.

    A lake GEBCO has no soundings for sits at one flat value -- its own
    surface -- so it is recognised by how far the bed falls below the surface
    and by having any relief at all. Lakes listed in lake-sources.json are kept
    whatever GEBCO says: their bed comes from their own raster.
    """
    if skip_if_exists(dst):
        return dst
    keep_names = keep_names or set()
    keep_ids = keep_ids or set()
    kept = []
    with rasterio.open(gebco) as dem, fiona.open(band) as lakes, Progress() as progress:
        task = progress.add_task("Checking lakes against GEBCO", total=len(lakes))
        for feature in lakes:
            progress.advance(task)
            geom = shape(feature["geometry"])
            surface = feature["properties"]["elev_m"]
            if feature["properties"].get("name") in keep_names:
                kept.append(feature)
                continue
            if surface is None:
                continue
            window = from_bounds(*geom.bounds, transform=dem.transform)
            bed = dem.read(1, window=window, boundless=True, fill_value=32767).astype("float32")
            if bed.size == 0:
                continue
            inside = ~geometry_mask(
                [geom], out_shape=bed.shape, transform=dem.window_transform(window),
                invert=False,
            )
            bed = bed[inside & (bed < 32767)]
            if bed.size < 4:
                continue
            depth = surface - float(bed.min())
            relief = float(bed.max()) - float(bed.min())
            if depth >= LAKE_RELIEF_DEPTH and relief >= LAKE_RELIEF_DEPTH:
                kept.append(feature)

    with fiona.open(band) as lakes:
        with fiona.open(dst, "w", driver="FlatGeobuf", crs=lakes.crs,
                        schema=lakes.schema, layer="lakes") as out:
            out.writerecords(kept)
    console.print(f"  [green]{len(kept)}[/green] lakes have a surveyed bed in GEBCO")
    return dst


def split_by_elevation(source: Path, work: Path) -> dict[str, Path]:
    """Split lakes into coastal and inland, by surface elevation.

    A lake at sea level sits against the sea and should read like it, so its
    bed takes the sea's ramp; an inland lake takes the lake ramp. Two files,
    so each renders with its own ramp into its own archive. Each archive is
    opaque only inside its own lakes, so they cannot paint over each other.
    """
    def coastal(p) -> bool:
        """Low *and* shallow: a lagoon, not a deep lake that happens to be low."""
        elev, depth = p.get("elev_m"), p.get("depth_avg")
        return (elev is not None and elev <= COASTAL_LAKE_ELEV
                and (depth is None or abs(depth) <= COASTAL_LAKE_DEPTH))

    out = {}
    with fiona.open(source) as src:
        for kind, keep in (("coastal", coastal), ("inland", lambda p: not coastal(p))):
            dst = work / "lakes" / f"lakes_surveyed_{kind}.fgb"
            out[kind] = dst
            if skip_if_exists(dst):
                continue
            features = [f for f in src if keep(f["properties"])]
            with fiona.open(dst, "w", driver="FlatGeobuf", crs=src.crs,
                            schema=src.schema, layer="lakes") as sink:
                sink.writerecords(features)
            console.print(f"  {len(features)} {kind} lakes with a surveyed bed")
    return out


def bed_ceiling(ramp: Ramp) -> float:
    """The strongest highlight this ramp can take without clipping to white.

    A lake bed is lit like any other relief, and the shallow end of both bed
    ramps is nearly white: lakes.txt reaches 236 and bathymetry-water.txt 245.
    A fixed 1.15 ceiling put the first at 284 and the second at 282, so every
    lit slope in shallow water clipped -- below 51 m for an inland lake and
    below 300 m for a coastal one, which is all of them. The glow inside the
    Alqueva reservoir was that, not a depth error.

    Deriving the ceiling instead of fixing it means the coastal beds keep the
    sea's own ramp, so a liman still reads as the water it joins, and a future
    change to either ramp cannot quietly start clipping again. The cap of 1.08
    is the land's, which is as much light as a pale surface wants.
    """
    # 254, not 255: the shading rounds, and a ceiling that lands exactly on
    # 255 still clips.
    return min(1.08, 254.0 / max(1.0, float(ramp.colour(ramp.elevation).max())))


def relief_spec(
    base: Path, lakes_fgb: Path, ramp: str, extra: list[Source] | None = None
) -> RenderSpec:
    """Render lake beds as depth below each lake's own surface.

    Rasters from lake-sources.json come first; GEBCO fills in behind them.
    """
    stack = (*(extra or []), gebco_source(base))
    parsed = Ramp.parse((RAMP_DIR / ramp).read_text())
    return RenderSpec(
        bands=(Band(LAKE_RELIEF_ZOOMS[0], LAKE_RELIEF_ZOOMS[1], stack),),
        ramp=parsed,
        # Lake beds are gentler than the seabed and much smaller on screen.
        shading=Shading(strength=0.5, reference_m=40.0, power=0.5, floor=0.65,
                        ceiling=bed_ceiling(parsed)),
        mask="inside",
        mask_path=str(lakes_fgb),
        datum=Datum(str(lakes_fgb), "elev_m"),
        # Depth below the surface: anything at or above it is not lake bed.
        clamp=(-12000, 0),
    )


def relief_tiles(lakes_fgb: Path) -> list[tuple[int, int, int]]:
    """Every tile over a surveyed lake, across the relief zooms."""
    lo, hi = LAKE_RELIEF_ZOOMS
    wanted: set[tuple[int, int, int]] = set()
    with fiona.open(lakes_fgb) as lakes:
        for feature in lakes:
            bounds = shape(feature["geometry"]).bounds
            for z in range(lo, hi + 1):
                wanted.update((z, x, y) for x, y in tiles_in_bbox(bounds, z))
    return sorted(wanted)


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05c: Generate Lake Tiles[/bold]\n")

    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = ensure_dir(get_tile_dir(base))

    # Naming a subset renders those archives alone, as the bathymetry and land
    # steps do. It is not only a convenience here: the lake beds need none of
    # the vector work below, and the N50 pass and the bands shell out to
    # ogr2ogr, which the remote build image does not carry -- it has only the
    # GDAL inside the rasterio and fiona wheels. Without this, a bed rebuild
    # dies in Step 1b on a geodatabase the beds never read.
    import os

    wanted = set(os.environ.get("BASEMAP_LAKE_ARCHIVES", "").split())
    beds_only = bool(wanted) and wanted <= {"lake_relief", "lake_relief_coastal"}

    console.print("=== Step 1: HydroLAKES ===")
    shp = source_shapefile(work)

    if beds_only:
        console.print("  beds only: skipping N50, the bands and the vector archive")
        pmtiles = tile_dir / "lakes.pmtiles"
    else:
        console.print("=== Step 1b: N50 lakes for Norway ===")
        extra, superseded = n50_lakes(work, shp)

        console.print("=== Step 2: Bands by lake area ===")
        inputs = []
        for min_area, (lo, hi) in LAKE_BANDS:
            inputs.append({
                "file": band_file(shp, work, min_area, extra, superseded), "layer": "lakes",
                "minzoom": lo, "maxzoom": hi,
            })

        console.print("=== Step 3: PMTiles ===")
        pmtiles = tile_dir / "lakes.pmtiles"
        if not skip_if_exists(pmtiles):
            generate_pmtiles_multilayer(
                inputs, pmtiles,
                min_zoom=LAKE_BANDS[0][1][0], max_zoom=LAKE_BANDS[-1][1][1],
                detail=SHORELINE_DETAIL,
                extra_args=[
                    "--no-feature-limit",
                    "--no-tile-size-limit",
                    "--detect-shared-borders",
                    "--simplification=2",
                    "--simplify-only-low-zooms",
                ],
            )

    console.print("=== Step 4: Lake beds ===")
    extra, names, ids = external_sources(work)
    surveyed = surveyed_lakes(
        band_file(shp, work, LAKE_RELIEF_AREA), gebco_source(base).uri,
        work / "lakes" / "lakes_surveyed.fgb", names, ids,
    )
    parts = split_by_elevation(surveyed, work)
    built = []
    for kind, ramp in (("inland", "lakes.txt"), ("coastal", "bathymetry-water.txt")):
        name = "lake_relief" if kind == "inland" else "lake_relief_coastal"
        if wanted and name not in wanted:
            continue
        relief = tile_dir / f"{name}.pmtiles"
        built.append(relief)
        if skip_if_exists(relief):
            continue
        spec = relief_spec(base, parts[kind], ramp, extra)
        render_into(stage_path(relief), spec, relief_tiles(parts[kind]), label=name)
        finalize(stage_path(relief), relief, name=name,
                 attribution="GEBCO Compilation Group; HydroLAKES (HydroSHEDS)")

    console.print(f"[green]Done:[/green] {pmtiles}, {', '.join(p.name for p in built)}")
