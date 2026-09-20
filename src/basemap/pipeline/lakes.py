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

import zipfile
from pathlib import Path

import fiona
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from rich.console import Console
from rich.progress import Progress
from shapely.geometry import shape

from basemap.config import (
    COASTAL_LAKE_ELEV,
    HYDROLAKES_URL,
    LAKE_BANDS,
    LAKE_RELIEF_AREA,
    LAKE_RELIEF_DEPTH,
    LAKE_RELIEF_ZOOMS,
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

# The attributes worth carrying into the tiles, renamed to the style's casing.
FIELDS = (
    "Lake_name AS name",
    "Lake_area AS area_km2",
    "Depth_avg AS depth_avg",
    "Elevation AS elev_m",
)


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


def band_file(shp: Path, work: Path, min_area: float) -> Path:
    """The lakes at least *min_area* km2 across, as FlatGeobuf."""
    name = "all" if min_area <= 0 else f"{min_area:g}"
    out = work / "lakes" / f"lakes_{name}.fgb"
    if not skip_if_exists(out):
        where = "" if min_area <= 0 else f" WHERE Lake_area >= {min_area}"
        ogr2ogr(
            shp, out, output_format="FlatGeobuf", nln="lakes",
            sql=f"SELECT {', '.join(FIELDS)} FROM HydroLAKES_polys_v10{where}",
        )
    return out


def surveyed_lakes(band: Path, gebco: Path, dst: Path) -> Path:
    """Keep the lakes whose bed GEBCO actually holds, with their surface.

    A lake GEBCO has no soundings for sits at one flat value -- its own
    surface -- so it is recognised by how far the bed falls below the surface
    and by having any relief at all.
    """
    if skip_if_exists(dst):
        return dst
    kept = []
    with rasterio.open(gebco) as dem, fiona.open(band) as lakes, Progress() as progress:
        task = progress.add_task("Checking lakes against GEBCO", total=len(lakes))
        for feature in lakes:
            progress.advance(task)
            geom = shape(feature["geometry"])
            surface = feature["properties"]["elev_m"]
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
    out = {}
    with fiona.open(source) as src:
        for kind, keep in (
            ("coastal", lambda e: e is not None and e <= COASTAL_LAKE_ELEV),
            ("inland", lambda e: e is None or e > COASTAL_LAKE_ELEV),
        ):
            dst = work / "lakes" / f"lakes_surveyed_{kind}.fgb"
            out[kind] = dst
            if skip_if_exists(dst):
                continue
            features = [f for f in src if keep(f["properties"].get("elev_m"))]
            with fiona.open(dst, "w", driver="FlatGeobuf", crs=src.crs,
                            schema=src.schema, layer="lakes") as sink:
                sink.writerecords(features)
            console.print(f"  {len(features)} {kind} lakes with a surveyed bed")
    return out


def relief_spec(base: Path, lakes_fgb: Path, ramp: str) -> RenderSpec:
    """Render lake beds from GEBCO, as depth below each lake's own surface."""
    return RenderSpec(
        bands=(Band(LAKE_RELIEF_ZOOMS[0], LAKE_RELIEF_ZOOMS[1], (gebco_source(base),)),),
        ramp=Ramp.parse((RAMP_DIR / ramp).read_text()),
        # Lake beds are gentler than the seabed and much smaller on screen.
        shading=Shading(strength=0.5, reference_m=40.0, power=0.5, floor=0.65, ceiling=1.15),
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

    console.print("=== Step 1: HydroLAKES ===")
    shp = source_shapefile(work)

    console.print("=== Step 2: Bands by lake area ===")
    inputs = []
    for min_area, (lo, hi) in LAKE_BANDS:
        inputs.append({
            "file": band_file(shp, work, min_area), "layer": "lakes",
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

    console.print("=== Step 4: Lake beds from GEBCO ===")
    surveyed = surveyed_lakes(
        band_file(shp, work, LAKE_RELIEF_AREA), gebco_source(base).uri,
        work / "lakes" / "lakes_surveyed.fgb",
    )
    parts = split_by_elevation(surveyed, work)
    built = []
    for kind, ramp in (("inland", "lakes.txt"), ("coastal", "bathymetry-emodnet.txt")):
        name = "lake_relief" if kind == "inland" else "lake_relief_coastal"
        relief = tile_dir / f"{name}.pmtiles"
        built.append(relief)
        if skip_if_exists(relief):
            continue
        spec = relief_spec(base, parts[kind], ramp)
        render_into(stage_path(relief), spec, relief_tiles(parts[kind]), label=name)
        finalize(stage_path(relief), relief, name=name,
                 attribution="GEBCO Compilation Group; HydroLAKES (HydroSHEDS)")

    console.print(f"[green]Done:[/green] {pmtiles}, {', '.join(p.name for p in built)}")
