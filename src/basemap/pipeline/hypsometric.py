"""Pipeline step 06h: Copernicus DEM → shaded land relief PMTiles.

The relief used to come from GEBCO, whose land is a 463 m grid: blurred at
z10, and its land mask -- rasterised on that same grid -- stepped over the
shoreline in blocks. It now comes from the Copernicus DEM, 30 m across the
European extent the bathymetry is detailed in and 90 m elsewhere, coloured on
the EMODnet land ramp with the hillshade baked in.

The tiles need no land mask at all. The viewer draws them under the vector
sea mask, which hides their sea part along the exact OSM coastline, so the
only thing that decides where land ends is the vector edge.

    land_relief     z0-7    GEBCO                       (land tiles only)
                    z8-10   GLO-30, then GLO-90, then GEBCO
    land_relief_eu  z11-12  GLO-30, then GEBCO          (EMODnet extent only)

GLO-30's native pixel is about z12, so MapLibre overzooms it beyond that.
"""

from __future__ import annotations

import json
import subprocess
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
from basemap.pipeline.bathymetry import gebco_source
from basemap.processing import copernicus
from basemap.processing.clip_land import require_ne_land, require_osm_land
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

# Keep low coastal hills legible without amplifying the surface model's fine
# texture into rocky terrain. At Cabo Espichel a z12 pixel is ~30 m: the old
# 6 m reference exaggerated slopes by ~2x even at the DEM's native scale.
# The lower highlight ceiling also leaves headroom for the pale land colours
# (the old 1.18 multiplier clipped channels above 216 to white).
LAND_SHADING = Shading(
    strength=0.60, reference_m=20.0, power=0.42, floor=0.76, ceiling=1.08,
)


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


def specs(base: Path) -> dict[str, RenderSpec]:
    work = get_work_dir(base)
    dem_dir = get_land_dem_dir(base)
    ramp = Ramp.parse((RAMP_DIR / "land-emodnet.txt").read_text())
    ice = Ramp.parse((RAMP_DIR / "ice.txt").read_text())
    ice_path = str(ice_polygons(work))
    land = str(require_osm_land(work))
    low = str(require_ne_land(work))
    gebco = gebco_source(base)
    glo30 = mosaics(dem_dir, "glo30", EMODNET_EXTENT)
    glo90 = mosaics(dem_dir, "glo90")

    # Retain cubic resampling: the earlier z11/z12 comparison found it kept
    # more detail than bilinear without nearest-neighbour aliasing. Adjust
    # lighting independently instead of blurring the DEM to soften the map.
    def spec(*bands: Band) -> RenderSpec:
        return RenderSpec(
            bands, ramp, LAND_SHADING, mask="land", mask_path=land, mask_path_low=low,
            ice_ramp=ice, ice_path=ice_path, clamp=(0, 9000),
            resampling="cubic",
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
    for name, zooms, bbox in (
        ("land_relief", range(0, 11), None),
        ("land_relief_eu", range(11, 13), EMODNET_EXTENT),
    ):
        dst = tile_dir / f"{name}.pmtiles"
        if skip_if_exists(dst):
            continue
        world = (-180.0, -85.0511287798066, 180.0, 85.0511287798066)
        tiles = [(z, x, y) for z in zooms for x, y in tiles_in_bbox(bbox or world, z)]
        render_into(stage_path(dst), all_specs[name], tiles, label=name)
        finalize(stage_path(dst), dst, name=name, attribution=ATTRIBUTION,
                 **({"bounds": bbox} if bbox else {}))

    console.print("[green]Done.[/green]")
