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
from rasterio.warp import transform_bounds
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rich.console import Console

from basemap.config import (
    EMODNET_EXTENT,
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


def bluetopo_sources(work: Path) -> list[Source]:
    """Staged BlueTopo grids as sources, finest first.

    No sign probing, unlike the EMODnet grids: BlueTopo stores elevation
    negative-down, the same convention as GEBCO, and carries its own compound
    CRS -- NAD83/UTM plus NAVD88, which is orthometric and so sits in GEBCO's
    stack without a chart-datum offset. The staging step has already masked
    everything the attribute table grades as fill rather than survey, so what
    is left is measurement and the gaps fall through to GEBCO.
    """
    grids = sorted((work / "bluetopo" / "grids").glob("*.tif"))
    if not grids:
        return []
    found = []
    for grid in grids:
        with rasterio.open(grid) as ds:
            bounds = tuple(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
            res = abs(ds.res[0])
        found.append((res, Source(str(grid), bounds=bounds)))
    console.print(f"  {len(found)} BlueTopo grids")
    return [source for _, source in sorted(found, key=lambda f: f[0])]


def specs(base: Path) -> dict[str, RenderSpec]:
    """The render spec for each archive whose sources are on disk."""
    work = get_work_dir(base)
    from basemap.config import water_ramp_file

    ramp = Ramp.parse(water_ramp_file().read_text())
    # Tuned on the Lisbon and Setubal canyons: enough to lift the shelf, while
    # the canyon walls shade without going black.
    #
    # The ceiling is what the ramp's headroom was chosen against. Shading is a
    # multiply that clips at 255, so a ceiling above 255/240 -- the ramp's
    # brightest channel -- blows the lit slopes out to white and takes the hue
    # with them. At 1.12 only genuinely bright slopes clip; at the old 1.2 an
    # eighth of the inner Oslofjord did.
    shading = Shading(strength=0.6, reference_m=30.0, power=0.5, floor=0.6, ceiling=1.12)
    land = str(require_osm_land(work))
    low = str(require_ne_land(work))
    gebco = gebco_source(base)

    # The sea mask is the coastline alone. Observed surface water used to be
    # folded in here so that lagoons stopped being land, and it worked as a
    # mask and failed as a map: the bathymetry ramp is near-white in its first
    # two metres, so the Venice lagoon came out the tone of the pale land
    # beside it. Coastal water is now drawn by the flat-tile compositor, as
    # inland water with its own flat blue, on the land side of this mask --
    # see COASTAL_WATER_RGB in pipeline/xyz.py. Keeping it out of here is what
    # leaves a land side for it to be drawn on.
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
    hr = hr_sources(work, emodnet) + bluetopo_sources(work)
    if hr:
        out["bathy_hr"] = spec(Band(*HR_ZOOMS, (*hr, emodnet, gebco), primary=len(hr)))
    else:
        console.print("  [dim]No HR grids (run the hr-bathymetry step): skipping bathy_hr.[/dim]")
    return out


def global_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in range(9) for x in range(1 << z) for y in range(1 << z)]


def eu_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in range(9, 12) for x, y in tiles_in_bbox(EMODNET_EXTENT, z)]


def hr_extent(spec: RenderSpec) -> dict:
    """The bounds bathy_hr actually covers, from its own sources.

    This was EMODnet's extent, which was true while EMODnet's grids were the
    only HR source and silently false the moment BlueTopo was added: the
    archive held California and its header said 36W to 43E, so a reader never
    asked for a tile there. The data was in the file and the map showed GEBCO.

    A declared box is not a free-form label -- the PMTiles reader skips
    anything outside it -- so it has to come from what was rendered.
    """
    from basemap.config import render_bbox

    band = spec.band(HR_ZOOMS[0])
    boxes = [s.bounds for s in band.sources[: band.primary] if s.bounds]
    if not boxes:
        return dict(bounds=EMODNET_EXTENT)
    covered = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    box = render_bbox()
    if box is not None:                       # a preview holds only its box
        covered = (max(covered[0], box[0]), max(covered[1], box[1]),
                   min(covered[2], box[2]), min(covered[3], box[3]))
    return dict(bounds=covered)


def hr_seed_tiles(spec: RenderSpec) -> list[tuple[int, int, int]]:
    """Every z12 tile inside some HR grid's rectangle.

    BASEMAP_RENDER_BBOX narrows that to a box, which is how a new HR source is
    judged before committing to the whole footprint: the CONUS grids seed some
    forty-six thousand z12 tiles and descend to z15, where California alone is
    a fraction of that and renders in about an hour. An archive built this way
    holds only the box and must not be promoted over the real one.
    """
    from basemap.config import render_bbox

    z = HR_ZOOMS[0]
    box = render_bbox()
    seeds: set[tuple[int, int]] = set()
    for source in spec.band(z).sources[: spec.band(z).primary]:
        seeds.update(tiles_in_bbox(source.bounds, z))
    if box is not None:
        inside = set(tiles_in_bbox(box, z))
        before = len(seeds)
        seeds &= inside
        console.print(f"  render box keeps {len(seeds)} of {before} z12 seeds")
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

    # A candidate palette only has to be judged on a whole map, not at survey
    # resolution, and bathy_hr is most of the cost: naming a subset here renders
    # the world to z11 in an hour instead of re-rendering the HR levels too.
    import os

    wanted = os.environ.get("BASEMAP_BATHY_ARCHIVES", "").split()

    for name, tiles, bounds in (
        ("bathy_global", global_tiles, {}),
        ("bathy_eu", eu_tiles, extent),
        ("bathy_hr", None, hr_extent(all_specs["bathy_hr"]) if "bathy_hr" in all_specs
         else extent),
    ):
        if name not in all_specs or (wanted and name not in wanted):
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
