"""Pipeline step: HydroRIVERS, for the river lines in the tree and the map.

The rivers were Natural Earth's centrelines: a few hundred of the world's
largest, chosen for a wall map. At z10 that drew the Rhône through the
Camargue and nothing else -- no Vidourle, no Petit Rhône, none of the
drainage that gives a delta its shape on EMODnet's map. HydroRIVERS
(HydroSHEDS, Lehner & Grill 2013) is the global river network derived from
the 90 m HydroSHEDS drainage, 8.5 million reaches, each with a Strahler
order and a modelled mean discharge. CC BY 4.0, credited as ATTRIBUTION.

Which reaches to draw at which zoom is one rule in SELECTION, used by both
the flat tree (pipeline/xyz.py) and the vector archive built here, so the two
maps show the same rivers. A reach is kept at a zoom if its order or its
discharge clears the bar: order alone drops the lower Nile below the Aswan
dam, which has few tributaries; discharge alone keeps every braid of a
mountain torrent.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.download import download_file
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import available, generate_pmtiles_multilayer

console = Console()

URL = "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_shp.zip"
ATTRIBUTION = ("HydroRIVERS v1.0 (HydroSHEDS): Lehner & Grill (2013), "
               "doi:10.1002/hyp.9740, CC BY 4.0")
ARCHIVE = "rivers"
MIN_ZOOM = 4
MAX_ZOOM = 10            # the vector archive's deepest level; the viewer overzooms
# (up to and including this zoom, least Strahler order, least mean discharge
# in m³/s): a reach is drawn when either is met. The last row serves every
# zoom beyond it, which is where the tree's z11 and z12 read from.
SELECTION: tuple[tuple[int, int, float], ...] = (
    (5, 7, 1000.0),      # the Rhône, the Po, the Ebro: what a continent shows
    (6, 6, 400.0),
    (7, 6, 200.0),
    (8, 5, 100.0),
    (9, 5, 50.0),
    (10, 4, 20.0),
)


def selection(z: int) -> tuple[int, float] | None:
    """(least order, least discharge) drawn at *z*, or None below MIN_ZOOM."""
    if z < MIN_ZOOM:
        return None
    for max_zoom, order, discharge in SELECTION:
        if z <= max_zoom:
            return order, discharge
    return SELECTION[-1][1], SELECTION[-1][2]


def wanted(order: int, discharge: float, z: int) -> bool:
    rule = selection(z)
    if rule is None:
        return False
    return order >= rule[0] or discharge >= rule[1]


def rivers_fgb(work: Path) -> Path:
    return work / "land" / "hydrorivers.fgb"


def _shapefile(work: Path) -> Path:
    src = ensure_dir(work / "hydrorivers")
    shp = src / "HydroRIVERS_v10_shp" / "HydroRIVERS_v10.shp"
    if shp.is_file():
        return shp
    archive = download_file(URL, src / "HydroRIVERS_v10_shp.zip", label="HydroRIVERS")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(src)
    if not shp.is_file():
        found = sorted(src.rglob("HydroRIVERS_v10.shp"))
        if not found:
            raise SystemExit(f"HydroRIVERS_v10.shp not in {archive}")
        shp = found[0]
    return shp


def stage(work: Path) -> Path:
    """The reaches the deepest zoom draws, as one FlatGeobuf with an index.

    Two attributes travel: the Strahler order and the mean discharge, which is
    all the selection and the pen widths read. The 8.5 million reaches come
    down to the few hundred thousand that clear the deepest zoom's bar, which
    is what makes a bbox query per block of tiles cheap enough for the tree.
    """
    import fiona

    out = rivers_fgb(work)
    if out.is_file():
        return out
    shp = _shapefile(work)
    ensure_dir(out.parent)
    tmp = out.with_suffix(".part.fgb")
    schema = {"geometry": "LineString", "properties": {"order": "int", "dis": "float"}}
    kept = total = 0
    with fiona.open(shp) as src, Progress(console=console) as progress, \
            fiona.open(tmp, "w", driver="FlatGeobuf", schema=schema, crs=src.crs) as dst:
        task = progress.add_task("HydroRIVERS", total=len(src))
        for feature in src:
            total += 1
            progress.advance(task)
            props = feature["properties"]
            order = int(props.get("ORD_STRA") or 1)
            discharge = float(props.get("DIS_AV_CMS") or 0.0)
            if not wanted(order, discharge, MAX_ZOOM):
                continue
            geometry = feature["geometry"]
            if not geometry or geometry["type"] != "LineString":
                continue
            dst.write({"geometry": geometry,
                       "properties": {"order": order, "dis": discharge}})
            kept += 1
    tmp.replace(out)
    console.print(f"  {kept:,} of {total:,} reaches kept")
    return out


def band_file(work: Path, fgb: Path, lo: int, hi: int) -> Path:
    """The reaches a zoom band draws, as its own file for tippecanoe."""
    import fiona

    out = ensure_dir(work / "hydrorivers") / f"rivers_z{lo}-{hi}.fgb"
    if out.is_file():
        return out
    tmp = out.with_suffix(".part.fgb")
    with fiona.open(fgb) as src, \
            fiona.open(tmp, "w", driver="FlatGeobuf", schema=src.schema, crs=src.crs) as dst:
        for feature in src:
            p = feature["properties"]
            if wanted(int(p["order"]), float(p["dis"]), hi):
                dst.write(feature)
    tmp.replace(out)
    return out


def bands() -> list[tuple[int, int]]:
    """Zoom bands with one selection rule each."""
    out, lo = [], MIN_ZOOM
    for max_zoom, _, _ in SELECTION:
        out.append((lo, max_zoom))
        lo = max_zoom + 1
    return out


def archive(work: Path, tile_dir: Path, fgb: Path) -> Path | None:
    """The vector archive, or None where tippecanoe cannot run.

    The tree reads the staged file and never this archive, so a host without
    a working tippecanoe -- the build Job, whose image cannot load the binary
    built for this workspace -- still stages what the tree needs. The archive
    is then built where tippecanoe works and promoted beside the others. Said
    loudly, because a viewer deployed without it has a river layer that 404s.
    """
    pmtiles = tile_dir / f"{ARCHIVE}.pmtiles"
    if skip_if_exists(pmtiles):
        return pmtiles
    if not available():
        console.print(f"  [yellow]tippecanoe cannot run here: {pmtiles.name} is NOT built. "
                      f"Build it with `oceanbase run rivers` where it can and promote it.[/yellow]")
        return None
    inputs = [{"file": band_file(work, fgb, lo, hi), "layer": ARCHIVE,
               "minzoom": lo, "maxzoom": hi} for lo, hi in bands()]
    generate_pmtiles_multilayer(
        inputs, pmtiles, min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM,
        extra_args=["--no-feature-limit", "--no-tile-size-limit",
                    "--simplification=4", "--simplify-only-low-zooms",
                    f"--attribution={ATTRIBUTION}"],
    )
    return pmtiles


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05g: Rivers (HydroRIVERS)[/bold]\n")
    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    console.print("=== Step 1: Reaches ===")
    fgb = stage(work)
    console.print("=== Step 2: PMTiles ===")
    pmtiles = archive(work, ensure_dir(get_tile_dir(base)), fgb)
    console.print(f"[green]Done:[/green] {fgb}" + (f", {pmtiles}" if pmtiles else ""))
