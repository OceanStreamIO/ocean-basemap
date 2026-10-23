"""Pipeline step: the river lines in the tree and the map, from MERIT Hydro.

The rivers were Natural Earth's centrelines: a few hundred of the world's
largest, chosen for a wall map. At z10 that drew the Rhône through the
Camargue and nothing else. HydroRIVERS replaced them, and its licence
(HydroSHEDS v1, WWF) then kept the tiles from going out under CC BY 4.0. They
now come from the network pipeline/merit_rivers.py traces from MERIT Hydro
(ODbL 1.0): stretches between confluences, each with a Strahler order and a
mean discharge estimated from G-RUN ENSEMBLE runoff. Credited as ATTRIBUTION.

Which stretches to draw at which zoom is one rule in SELECTION, used by both
the flat tree (pipeline/xyz.py) and the vector archive built here, so the two
maps show the same rivers. A stretch is kept at a zoom if its order or its
discharge clears the bar: order alone misses the short, full rivers of a wet
coast; discharge alone drops the dry channels of a desert. Those are kept,
and drawn dashed: a channel carrying under DRY_DIS is dry for most of the
year, and a flood map needs to know where it runs.

The orders are HydroRIVERS'. Measured over the world (processing/river_lengths.py),
MERIT's orders draw 93-138 % of HydroRIVERS' length region by region at the
same thresholds: they agree at the scale of a continent. They differ on wet
coasts -- Kodiak kept 2 of its 6 rivers -- which is what the discharge bar is
for, so the discharge bars are half HydroRIVERS'. That draws 109-153 % of
HydroRIVERS' length region by region, and five of Kodiak's six rivers.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.paths import ensure_dir, skip_if_exists
from basemap.utils.tippecanoe import available, generate_pmtiles_multilayer

console = Console()

ATTRIBUTION = ("Contains information from MERIT Hydro (Yamazaki et al. 2019, "
               "doi:10.1029/2019WR024873), made available under the ODbL 1.0; "
               "discharge from G-RUN ENSEMBLE (Ghiggi et al. 2021, "
               "doi:10.1029/2020WR028787), CC BY 4.0")
ARCHIVE = "rivers"
MIN_ZOOM = 4
MAX_ZOOM = 10            # the vector archive's deepest level; the viewer overzooms
# (up to and including this zoom, least Strahler order, least mean discharge
# in m³/s): a stretch is drawn when either is met. The last row serves every
# zoom beyond it, which is where the tree's z11 and z12 read from.
SELECTION: tuple[tuple[int, int, float], ...] = (
    (5, 7, 500.0),       # the Rhône, the Po, the Ebro: what a continent shows
    (6, 6, 200.0),
    (7, 6, 100.0),
    (8, 5, 50.0),
    (9, 5, 20.0),
    (10, 4, 10.0),
)
# The rule HydroRIVERS was drawn with until 2026-10, for comparing the two
# (oceanbase river-compare).
HYDRORIVERS_SELECTION: tuple[tuple[int, int, float], ...] = (
    (5, 7, 1000.0),
    (6, 6, 400.0),
    (7, 6, 200.0),
    (8, 5, 100.0),
    (9, 5, 50.0),
    (10, 4, 20.0),
)
# Mean discharge under which a channel is drawn dry (dashed), m³/s.
DRY_DIS = 0.5


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
    return work / "land" / "rivers.fgb"


def _network(work: Path) -> Path:
    from basemap.pipeline.merit_rivers import is_current, network_fgb

    fgb = network_fgb(work)
    if not fgb.is_file() or not is_current(fgb):
        raise SystemExit(f"missing or outdated {fgb}; run the merit-rivers step first, or "
                         "stage it from basemap-build/out/rivers/")
    return fgb


def _land(work: Path) -> Path:
    from basemap.processing.clip_land import require_osm_land

    return require_osm_land(work)


def stage(work: Path) -> Path:
    """The stretches the deepest zoom draws, as one FlatGeobuf with an index.

    Two attributes travel: the Strahler order and the mean discharge, which is
    all the selection, the pens and the dry dashes read. The seven million
    stretches come down to the ones that clear the deepest zoom's bar, which
    is what makes a bbox query per block of tiles cheap enough for the tree.
    They are then cut at the coastline the tiles are drawn with (clip_to_land).
    """
    import fiona
    import numpy as np
    import shapely
    from shapely.geometry import mapping, shape

    out = rivers_fgb(work)
    if out.is_file():
        return out
    network = _network(work)
    land = _land(work)
    ensure_dir(out.parent)
    geoms, orders, dis = [], [], []
    total = 0
    with fiona.open(network) as src, Progress(console=console) as progress:
        crs = src.crs
        task = progress.add_task("Stretches", total=len(src))
        for feature in src:
            total += 1
            progress.advance(task)
            props = feature["properties"]
            order = int(props.get("order") or 0)
            discharge = float(props.get("dis") or 0.0)
            if not wanted(order, discharge, MAX_ZOOM):
                continue
            geometry = feature["geometry"]
            if not geometry or geometry["type"] != "LineString":
                continue
            geoms.append(shape(geometry))
            orders.append(order)
            dis.append(discharge)
    console.print(f"  {len(geoms):,} of {total:,} stretches kept; cutting them at the coastline")
    which, parts = clip_to_land(np.asarray(geoms, dtype=object), land)
    tmp = out.with_suffix(".part.fgb")
    tmp.unlink(missing_ok=True)
    schema = {"geometry": "LineString", "properties": {"order": "int", "dis": "float"}}
    with fiona.open(tmp, "w", driver="FlatGeobuf", schema=schema, crs=crs) as dst:
        batch = []
        for i, part in zip(which.tolist(), parts):
            batch.append({"geometry": mapping(part),
                          "properties": {"order": orders[i], "dis": dis[i]}})
            if len(batch) >= 50_000:
                dst.writerecords(batch)
                batch = []
        if batch:
            dst.writerecords(batch)
    tmp.replace(out)
    before = float(shapely.length(np.asarray(geoms, dtype=object)).sum())
    after = float(shapely.length(parts).sum()) if len(parts) else 0.0
    console.print(f"  {len(parts):,} lines written; {1 - after / max(before, 1e-12):.2%} "
                  "of the length lay over the sea and was cut")
    return out


def clip_to_land(lines, land: Path, chunk: int = 20_000):
    """Cut lines at the coastline in *land* (polygons): (source index, part) pairs.

    MERIT carries its rivers on down fjords and estuaries that its own land
    mask counts as land -- the Drammenselva runs on along the middle of the
    Drammensfjord, the Garonne down the Gironde -- where the tiles show sea.
    The land is OSM's, split into small polygons, the same the coastline is
    drawn from, so a river now ends where the map's shore is.

    A line inside one land polygon is kept whole without any arithmetic, which
    is nearly all of them. The rest are intersected with each polygon they
    touch, and the pieces joined again: the polygons tile the land without
    overlapping, so the pieces meet end to end at their seams.
    """
    import fiona
    import numpy as np
    import shapely
    from shapely.geometry import shape

    lines = np.asarray(lines, dtype=object)
    console.print(f"  reading the land polygons from {land.name}")
    with fiona.open(land) as src:
        polys = np.asarray([shape(f["geometry"]) for f in src if f["geometry"]], dtype=object)
    tree = shapely.STRtree(polys)
    out_idx: list[np.ndarray] = []
    out_geom: list[np.ndarray] = []
    for start in range(0, len(lines), chunk):
        part = lines[start:start + chunk]
        idx = np.arange(start, start + len(part))
        inside = np.zeros(len(part), bool)
        li, _ = tree.query(part, predicate="within")
        inside[li] = True
        out_idx.append(idx[inside])
        out_geom.append(part[inside])
        rest = np.flatnonzero(~inside)
        if not rest.size:
            continue
        li, pi = tree.query(part[rest], predicate="intersects")
        if not li.size:
            continue
        cut = shapely.intersection(part[rest][li], polys[pi])
        order = np.argsort(li, kind="stable")
        li, cut = li[order], cut[order]
        starts = np.r_[0, np.flatnonzero(li[1:] != li[:-1]) + 1]
        ends = np.r_[starts[1:], li.size]
        for s, e in zip(starts, ends):
            joined = shapely.line_merge(shapely.union_all(cut[s:e]))
            for piece in shapely.get_parts(joined):
                if piece.geom_type == "LineString" and piece.length > 0:
                    out_idx.append(np.array([idx[rest[li[s]]]]))
                    out_geom.append(np.array([piece], dtype=object))
    if not out_idx:
        return np.empty(0, np.int64), np.empty(0, dtype=object)
    return np.concatenate(out_idx), np.concatenate(out_geom)


def band_file(work: Path, fgb: Path, lo: int, hi: int) -> Path:
    """The stretches a zoom band draws, as its own file for tippecanoe."""
    import fiona

    out = ensure_dir(work / "rivers") / f"rivers_z{lo}-{hi}.fgb"
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


# Bumped when the staging itself changes, so an older staged file is redone.
STAGING = "clipped to the OSM coastline"


def _rule_key(network: Path, land: Path | None = None) -> str:
    import json

    files = [network] + ([land] if land is not None else [])
    stats = [[f.stat().st_size, int(f.stat().st_mtime)] for f in files]
    return json.dumps({"selection": SELECTION, "staging": STAGING, "inputs": stats})


def clear_stale(work: Path, tile_dir: Path) -> bool:
    """Remove what an earlier rule or network staged; True when anything went.

    The staged file, the band files and the archive are each skipped when
    present, so without this a changed rule would leave yesterday's rivers in
    place and say nothing.
    """
    network = _network(work)
    land = _land(work)
    mark = work / "rivers" / "rule.json"
    if mark.is_file() and mark.read_text() == _rule_key(network, land):
        return False
    stale = [rivers_fgb(work), tile_dir / f"{ARCHIVE}.pmtiles",
             *sorted((work / "rivers").glob("rivers_z*.fgb"))]
    gone = [f for f in stale if f.is_file()]
    for f in gone:
        f.unlink()
    ensure_dir(mark.parent)
    mark.write_text(_rule_key(network, land))
    if gone:
        console.print(f"  the rule or the network changed: cleared {len(gone)} staged file(s)")
    return bool(gone)


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 05g: Rivers (MERIT Hydro)[/bold]\n")
    base = get_data_dir(data_dir)
    work = get_work_dir(base)
    tile_dir = ensure_dir(get_tile_dir(base))
    clear_stale(work, tile_dir)
    console.print("=== Step 1: Stretches ===")
    fgb = stage(work)
    console.print("=== Step 2: PMTiles ===")
    pmtiles = archive(work, tile_dir, fgb)
    console.print(f"[green]Done:[/green] {fgb}" + (f", {pmtiles}" if pmtiles else ""))
