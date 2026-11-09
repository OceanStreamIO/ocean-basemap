"""Pipeline step 06h: Copernicus DEM → shaded land relief PMTiles.

The relief used to come from GEBCO, whose land is a 463 m grid: blurred at
z10, and its land mask -- rasterised on that same grid -- stepped over the
shoreline in blocks. It now comes from the Copernicus DEM, 30 m across the
European extent the bathymetry is detailed in and 90 m elsewhere, coloured on
the land ramp with the hillshade, the land-cover tint and the shoreline features
baked in (see processing/landcover.py).

The tiles need no land mask at all. The viewer draws them under the vector
sea mask, which hides their sea part along the exact OSM coastline, so the
only thing that decides where land ends is the vector edge.

    land_relief     z0-7    GEBCO                       (land tiles only)
                    z8-10   GLO-30, then GLO-90, then GEBCO
    land_relief_eu  z11-12  GLO-30, then GEBCO          (EMODnet extent only)

Tiles are rendered deeper and averaged down (LAND_OVERSAMPLE), and read the
deeper level's sources: z6 and z7 are averaged from z8, so they come from the
Copernicus DEM, not GEBCO.

GLO-30's native pixel is about z12, so MapLibre overzooms it beyond that.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console

from basemap.config import (
    EMODNET_EXTENT,
    ICE_SHAPEFILES,
    RAMP_DIR,
    get_data_dir,
    get_land_dem_dir,
    get_tile_dir,
    get_work_dir,
)
from basemap.pipeline import surface_water, worldcover
from basemap.pipeline.bathymetry import gebco_source
from basemap.processing import copernicus
from basemap.processing.clip_land import require_ne_land, require_osm_land
from basemap.processing.landcover import TINT_STRENGTH, LandCover
from basemap.processing.raster_tiles import (
    Band,
    Ramp,
    RenderSpec,
    Shading,
    Source,
    finalize,
    render_into,
    stage_path,
    tiles_in_bbox,
)
from basemap.utils.download import download_shapefile
from basemap.utils.paths import ensure_dir, skip_if_exists

console = Console()

ATTRIBUTION = "Copernicus DEM © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018"
# Arc-seconds per pixel at full resolution, and the COG overview levels each
# product ships, which the per-level mosaics read directly.
PRODUCTS = {"glo30": (1, 3), "glo90": (3, 2)}

# The published land treatment, adopted 2026-11-09 to bring the land closer to
# EMODnet's. What read as "rough" beside it was not fine detail -- ours had less
# -- but shadow: dark, grey, and cut off flat at the old 0.70 floor, with up to
# 31% of the Alps sitting on that limit. Four things changed together:
#
# The ramp is land-soft.txt: EMODnet's measured colours to 1,800 m, without the
# hard step to white above it that blotches Utah and Colorado on their map.
#
# The light is soft (see Shading): the factor eases into its limits instead of
# hitting them, shadows go warm rather than grey, and highlights lift toward
# white. The floor can then go lower, 0.56, without a band appearing on it. The
# slope gate drops to 2 degrees, because the old 4 left low hills unlit.
#
# Each tile is rendered deeper and averaged down, two levels to z6 and one to
# z11. z12 stays as it is: GLO-30's own pixel is about that size.
#
# The land-cover tint drops to 0.40 (processing/landcover.py), so forest stops
# reading as shadow.
#
# Other looks stay selectable with BASEMAP_LAND_STYLE: "m" is this one with the
# colour toned down, a third of the way back to the previous ramp, and "muted" is
# what was published from 2026-10-15 through v13. The rest are older candidates.
@dataclass(frozen=True)
class LandLook:
    """Everything that decides how the land is drawn, chosen as one."""

    ramp: str
    shading: Shading
    tint: float = TINT_STRENGTH
    oversample: tuple[tuple[int, int], ...] = ()


LAND_RAMP = "land-soft.txt"
LAND_SHADING = Shading(
    strength=0.90, reference_m=20.0, power=0.38, floor=0.56, ceiling=1.14,
    slope_gate_deg=2.0, soft=True, warm=0.18,
)
LAND_OVERSAMPLE = ((6, 2), (11, 1))
LAND_LOOK = LandLook(LAND_RAMP, LAND_SHADING, oversample=LAND_OVERSAMPLE)

_MUTED_SHADING = Shading(
    strength=0.95, reference_m=20.0, power=0.42, floor=0.70, ceiling=1.18,
    slope_gate_deg=4.0,
)


def _ungated(ramp: str, strength: float, ceiling: float) -> LandLook:
    """An older candidate: the hard, ungated light, at the old tint."""
    light = Shading(strength=strength, reference_m=20.0, power=0.42, floor=0.76, ceiling=ceiling)
    return LandLook(ramp, light, tint=0.67)


LAND_STYLES = {
    "c": LAND_LOOK,
    "m": LandLook("land-soft-muted.txt", LAND_SHADING, oversample=LAND_OVERSAMPLE),
    "muted": LandLook("land-muted.txt", _MUTED_SHADING, tint=0.67),
    "emodnet": _ungated("land-emodnet.txt", 0.60, 1.08),     # published until 2026-10-15
    "l1": _ungated("land-emodnet-warm.txt", 0.60, 1.08),
    "l3": _ungated("land-emodnet-warm.txt", 0.78, 1.16),
    "l4": _ungated("land-emodnet-mild.txt", 0.78, 1.16),
}


def download(dem_dir: Path) -> None:
    """Fetch GLO-30 inside the EMODnet extent and GLO-90 everywhere else."""
    dem_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        selected = copernicus.select(EMODNET_EXTENT, client, dem_dir)
        missing = {
            product: [t for t in tiles if not (dem_dir / _subdir(product) / f"{t}.tif").is_file()]
            for product, tiles in selected.items()
        }
        if any(missing.values()):
            copernicus.require_space(dem_dir.parent, sum(
                copernicus.projected_bytes(p, t, client) for p, t in missing.items() if t
            ))
    for product, tiles in selected.items():
        copernicus.fetch(product, tiles, dem_dir / _subdir(product))


def _subdir(product: copernicus.Product) -> str:
    return "glo30" if product is copernicus.GLO30 else "glo90"


def mosaics(dem_dir: Path, name: str, bounds: tuple | None = None) -> Source:
    """A source over one product's tiles, with a mosaic per COG overview level.

    ``-resolution highest`` matters: Copernicus tiles widen in longitude away
    from the equator, and the default averages them into a coarser grid. The
    widened tiles are then stretched to fit, which ``-r bilinear`` keeps smooth;
    the default nearest-neighbour leaves stripes the hillshade picks out.

    *bounds* limits the source to where its tiles are, so a tile elsewhere
    never pushes a continent-sized empty mosaic through the warper.
    """
    arcsec, levels = PRODUCTS[name]
    files = dem_dir / f"{name}.list"
    if not files.is_file():
        # Relative to the list, and built with cwd=dem_dir below, so GDAL writes
        # relativeToVRT="1" and the mosaic resolves against its own directory.
        # Absolute paths would pin twenty-six thousand tiles to this machine's
        # layout, and the same tree has to work on a cluster's shared filesystem
        # and on a compute node's local disk.
        names = sorted(q.name for q in (dem_dir / name).glob("*.tif"))
        files.write_text("\n".join(f"{name}/{q}" for q in names) + "\n")

    def vrt(level: int | None) -> str:
        path = dem_dir / (f"{name}.vrt" if level is None else f"{name}_ov{level}.vrt")
        if not path.is_file():
            args = [
                "gdalbuildvrt", "-q", "-resolution", "highest", "-r", "bilinear",
                "-input_file_list", files.name,
            ]
            if level is not None:
                args += ["-oo", f"OVERVIEW_LEVEL={level}"]
            subprocess.run([*args, path.name], check=True, cwd=dem_dir)
        return str(path)

    native = arcsec / 3600
    pyramid = tuple((native * 2 ** (level + 1), vrt(level)) for level in range(levels))
    return Source(vrt(None), pyramid=pyramid, bounds=bounds)


def ice_polygons(work: Path) -> Path:
    """Glaciers, ice caps and ice shelves as one file of polygons.

    Two Natural Earth layers merged: the glaciated areas, which carry the
    Greenland and Antarctic sheets along with every ice cap and mountain
    glacier, and the Antarctic ice shelves, which are floating and so are not
    in the first but are land to OSM, whose coastline follows the shelf edge.

    Written as GeoJSON rather than FlatGeobuf on purpose. Under a megabyte, it
    takes the path in land_in that reads a file once per worker and indexes it
    in memory, instead of the spatial-index query that suits OSM land at 1.3 GB.

    Read with fiona rather than ogr2ogr: this step and the bathymetry are the
    two the remote build Job runs, and that image carries only the GDAL inside
    the rasterio and fiona wheels -- no command-line tools at all.
    """
    import fiona
    from shapely.geometry import mapping, shape

    out = work / "ice" / "ice_10m.geojson"
    if out.is_file():
        return out
    ice_dir = ensure_dir(work / "ice")
    features: list[dict] = []
    for name, url in ICE_SHAPEFILES.items():
        shp = download_shapefile(name, url, ice_dir)
        with fiona.open(shp) as src:
            features += [
                {"type": "Feature", "properties": {}, "geometry": mapping(shape(f["geometry"]))}
                for f in src
                if f["geometry"]
            ]
    console.print(f"  {len(features)} ice polygons")
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return out


def land_cover_for(base: Path, tint: float = TINT_STRENGTH) -> LandCover | None:
    """The cover mosaic and the water occurrence the land tiles are tinted from.

    Both are required, loudly. A land archive rendered without the tint looks
    like a finished map and is not the one that was judged -- the same way a
    tree without lakes once shipped because missing data and absent data
    looked identical. Set BASEMAP_LAND_COVER=off to render plain land on
    purpose, for comparison.
    """
    import os

    if os.environ.get("BASEMAP_LAND_COVER", "").strip().lower() == "off":
        return None
    cover = worldcover.source(base)
    if cover is None:
        raise SystemExit("missing the WorldCover mosaic; run the worldcover step first")
    water = surface_water.source(base)
    if water is None:
        raise SystemExit("missing the surface-water mosaic; run the surface-water step first")
    return LandCover(cover=cover, water=water, tint_strength=tint)


def specs(base: Path) -> dict[str, RenderSpec]:
    work = get_work_dir(base)
    dem_dir = get_land_dem_dir(base)
    # Unset, this is the published look.
    import os

    style = os.environ.get("BASEMAP_LAND_STYLE", "").strip()
    if style and style not in LAND_STYLES:
        raise SystemExit(f"no such land style: {style}; have {sorted(LAND_STYLES)}")
    look = LAND_STYLES.get(style, LAND_LOOK)
    ramp = Ramp.parse((RAMP_DIR / look.ramp).read_text())
    ice = Ramp.parse((RAMP_DIR / "ice.txt").read_text())
    ice_path = str(ice_polygons(work))
    land = str(require_osm_land(work))
    low = str(require_ne_land(work))
    gebco = gebco_source(base)
    glo30 = mosaics(dem_dir, "glo30", EMODNET_EXTENT)
    glo90 = mosaics(dem_dir, "glo90")
    land_cover = land_cover_for(base, look.tint)

    # Retain cubic resampling: the earlier z11/z12 comparison found it kept
    # more detail than bilinear without nearest-neighbour aliasing. Adjust
    # lighting independently instead of blurring the DEM to soften the map.
    def spec(*bands: Band) -> RenderSpec:
        return RenderSpec(
            bands, ramp, look.shading, mask="land", mask_path=land, mask_path_low=low,
            ice_ramp=ice, ice_path=ice_path, clamp=(0, 9000),
            resampling="cubic", land_cover=land_cover, oversample=look.oversample,
        )

    return {
        "land_relief": spec(Band(0, 7, (gebco,)), Band(8, 10, (glo30, glo90, gebco))),
        "land_relief_eu": spec(Band(11, 12, (glo30, gebco))),
    }


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 06h: Generate Land Relief[/bold]\n")

    base = get_data_dir(data_dir)
    tile_dir = ensure_dir(get_tile_dir(base))

    console.print("=== Step 1: Copernicus DEM ===")
    download(ensure_dir(get_land_dem_dir(base)))

    console.print("=== Step 2: Render ===")
    all_specs = specs(base)
    # A candidate land palette is usually judged over Europe, where
    # land_relief_eu carries the detail; naming a subset renders that alone
    # rather than the global archive with it. Same idea as the sea candidates.
    import os

    wanted = os.environ.get("BASEMAP_LAND_ARCHIVES", "").split()

    for name, zooms, bbox in (
        ("land_relief", range(0, 11), None),
        ("land_relief_eu", range(11, 13), EMODNET_EXTENT),
    ):
        if wanted and name not in wanted:
            continue
        dst = tile_dir / f"{name}.pmtiles"
        if skip_if_exists(dst):
            continue
        world = (-180.0, -85.0511287798066, 180.0, 85.0511287798066)
        # A preview box narrows whatever this archive would otherwise cover.
        from basemap.config import render_bbox

        box = render_bbox()
        if box is not None:
            here = bbox or world
            bbox = (max(here[0], box[0]), max(here[1], box[1]),
                    min(here[2], box[2]), min(here[3], box[3]))
            if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                console.print(f"  [dim]{name}: outside the preview box, skipping[/dim]")
                continue
        tiles = [(z, x, y) for z in zooms for x, y in tiles_in_bbox(bbox or world, z)]
        render_into(stage_path(dst), all_specs[name], tiles, label=name)
        finalize(stage_path(dst), dst, name=name, attribution=ATTRIBUTION,
                 **({"bounds": bbox} if bbox else {}))

    console.print("[green]Done.[/green]")
