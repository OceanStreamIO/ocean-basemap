"""Pipeline step 07: continuous bathymetry → raster PMTiles.

The depth-zone polygons classify each grid into bands and vectorise them, so
every band edge is a staircase on the source grid and the seabed has no relief
of its own. This step renders the grids directly instead -- colour on a
continuous ramp with the hillshade baked in, and the sea as the alpha channel
so the tiles stop at the OSM coastline (see processing/raster_tiles).

Three archives, each serving the zooms its best source is good for, and each
tile completed from the sources beneath it where the best one has gaps:

    bathy_global   z0-8    GEBCO
    bathy_eu       z9-11   EMODnet DTM, then GEBCO    (EMODnet extent only)
    bathy_hr       z12-15  HR surveys, then the above (only where a survey is)

EMODnet and the HR surveys are opt-in downloads (``--with emodnet,hr-bathymetry``);
without them only the global archive is built.

The HR surveys are patchy inside their bounding rectangles, so their archive
is built one zoom at a time: a z12 tile is kept only when a survey reaches it,
and each deeper zoom renders just the children of the tiles kept above.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rich.console import Console

from basemap.config import (
    EMODNET_EXTENT,
    RAMP_DIR,
    get_data_dir,
    get_gebco_tif,
    get_tile_dir,
    get_work_dir,
)
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
    staged,
    tiles_in_bbox,
)
from basemap.utils.paths import ensure_dir, skip_if_exists

console = Console()

Bbox = tuple[float, float, float, float]

ATTRIBUTION = "GEBCO Compilation Group; EMODnet Bathymetry Consortium"
HR_ZOOMS = (12, 15)


def gebco_source(base: Path) -> Source:
    gebco = get_gebco_tif(base)
    if not Path(f"{gebco}.ovr").is_file():
        console.print(
            f"[red bold]ERROR:[/] {gebco.name} has no overviews, so low zooms would read the "
            f"whole grid.\n  Run: gdaladdo -ro -r average {gebco} 2 4 8 16 32 64 128"
        )
        raise SystemExit(1)
    return Source(str(gebco))


def emodnet_source(work: Path) -> Source:
    return Source(str(work / "emodnet" / "emodnet_mean.vrt"), bounds=EMODNET_EXTENT)


def grid_sign(uri: str, reference: str, samples: int = 256) -> int:
    """Return -1 when an HR grid stores depth positive-down, else 1.

    The grids disagree on the convention whatever their metadata says, so the
    sign has to be measured. It is measured pixel by pixel: the grid is read
    coarsely, the EMODnet DTM is warped onto exactly those pixels, and the sign
    that agrees with it better wins. Comparing medians over the grid's bounding
    box instead goes wrong on coastal surveys, whose box is mostly land: the
    reference median comes out positive and positive depths look right.
    """
    with rasterio.open(uri) as ds:
        step = max(1, max(ds.width, ds.height) // samples)
        shape = (max(1, ds.height // step), max(1, ds.width // step))
        grid = ds.read(1, out_shape=shape).astype("float32")
        transform = ds.transform * ds.transform.scale(ds.width / shape[1], ds.height / shape[0])
    dtm = np.full(shape, np.nan, dtype="float32")
    with rasterio.open(reference) as ref:
        reproject(
            rasterio.band(ref, 1), dtm, src_nodata=ref.nodata, dst_nodata=np.nan,
            dst_transform=transform, dst_crs="EPSG:4326", resampling=Resampling.bilinear,
        )
    both = np.isfinite(grid) & np.isfinite(dtm)
    if not both.any():
        return 1
    as_is = np.median(np.abs(grid[both] - dtm[both]))
    flipped = np.median(np.abs(-grid[both] - dtm[both]))
    return -1 if flipped < as_is else 1


def hr_sources(work: Path, emodnet: Source) -> list[Source]:
    """Every HR grid on disk as a source, finest first, with its sign resolved.

    Signs are measured against the local EMODnet mosaic and cached, since there
    are hundreds of grids and they don't change.
    """
    grids = sorted((work / "emodnet_hr" / "grids").glob("*.nc"))
    if not grids:
        return []
    cache_path = work / "emodnet_hr" / "grid_signs.json"
    cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}

    found = []
    for grid in grids:
        uri = f'NETCDF:"{grid}":elevation'
        with rasterio.open(uri) as ds:
            bounds = tuple(ds.bounds)
            res = abs(ds.res[0])
        if grid.name not in cache:
            cache[grid.name] = grid_sign(uri, emodnet.uri)
        found.append((res, Source(
            uri, sign=cache[grid.name], crs="EPSG:4326", bounds=bounds,
        )))
    cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True))
    flipped = sum(1 for v in cache.values() if v < 0)
    console.print(f"  {len(found)} HR grids ({flipped} stored positive-down, flipped)")
    return [source for _, source in sorted(found, key=lambda f: f[0])]


def specs(base: Path) -> dict[str, RenderSpec]:
    """The render spec for each archive whose sources are on disk."""
    work = get_work_dir(base)
    ramp = Ramp.parse((RAMP_DIR / "bathymetry-water.txt").read_text())
    # Tuned on the Lisbon and Setubal canyons: enough to lift the shelf, while
    # the canyon walls shade without going black.
    shading = Shading(strength=0.6, reference_m=30.0, power=0.5, floor=0.6, ceiling=1.2)
    land = str(require_osm_land(work))
    low = str(require_ne_land(work))
    gebco = gebco_source(base)

    def spec(*bands: Band) -> RenderSpec:
        return RenderSpec(
            bands, ramp, shading, mask="sea", mask_path=land, mask_path_low=low,
            clamp=(-12000, 0),
        )

    out = {"bathy_global": spec(Band(0, 8, (gebco,)))}
    emodnet = emodnet_source(work)
    if not Path(emodnet.uri).is_file():
        console.print("  [dim]No EMODnet mosaic (run the emodnet step): global archive only.[/dim]")
        return out
    out["bathy_eu"] = spec(Band(9, 11, (emodnet, gebco), primary=1))
    hr = hr_sources(work, emodnet)
    if hr:
        out["bathy_hr"] = spec(Band(*HR_ZOOMS, (*hr, emodnet, gebco), primary=len(hr)))
    else:
        console.print("  [dim]No HR grids (run the hr-bathymetry step): skipping bathy_hr.[/dim]")
    return out


def global_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in range(9) for x in range(1 << z) for y in range(1 << z)]


def eu_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in range(9, 12) for x, y in tiles_in_bbox(EMODNET_EXTENT, z)]


def hr_seed_tiles(spec: RenderSpec) -> list[tuple[int, int, int]]:
    """Every z12 tile inside some HR grid's rectangle."""
    z = HR_ZOOMS[0]
    seeds: set[tuple[int, int]] = set()
    for source in spec.band(z).sources[: spec.band(z).primary]:
        seeds.update(tiles_in_bbox(source.bounds, z))
    return [(z, x, y) for x, y in sorted(seeds)]


def build_hr(spec: RenderSpec, dst: Path) -> None:
    """Render the HR archive a zoom at a time, descending only where it has data."""
    stage = stage_path(dst)
    render_into(stage, spec, hr_seed_tiles(spec), label=f"{dst.stem} z{HR_ZOOMS[0]}")
    for z in range(HR_ZOOMS[0] + 1, HR_ZOOMS[1] + 1):
        parents = staged(stage, z - 1)
        children = [
            (z, 2 * x + dx, 2 * y + dy) for x, y in parents for dx in (0, 1) for dy in (0, 1)
        ]
        render_into(stage, spec, children, label=f"{dst.stem} z{z}")


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 07: Generate Continuous Bathymetry[/bold]\n")

    base = get_data_dir(data_dir)
    tile_dir = ensure_dir(get_tile_dir(base))
    all_specs = specs(base)
    extent = dict(bounds=EMODNET_EXTENT)

    for name, tiles, bounds in (
        ("bathy_global", global_tiles, {}),
        ("bathy_eu", eu_tiles, extent),
        ("bathy_hr", None, extent),
    ):
        if name not in all_specs:
            continue
        console.print(f"=== {name} ===")
        dst = tile_dir / f"{name}.pmtiles"
        if skip_if_exists(dst):
            continue
        if tiles is None:
            build_hr(all_specs[name], dst)
        else:
            render_into(stage_path(dst), all_specs[name], tiles(), label=name)
        finalize(stage_path(dst), dst, name=name, attribution=ATTRIBUTION, **bounds)

    console.print("[green]Done.[/green]")
