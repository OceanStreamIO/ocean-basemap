"""Pipeline step: the river network traced from MERIT Hydro.

HydroRIVERS drew the rivers until 2026-10, and its licence (HydroSHEDS v1,
WWF) does not let derived tiles go out under CC BY. MERIT Hydro (Yamazaki et
al. 2019, doi:10.1029/2019WR024873) is licensed under ODbL 1.0 as one of two
options, which allows commercial use and leaves maps made from it free to
license as we like; the network traced here is a derived database and is
published under ODbL itself.

MERIT is not a river map. It is a set of 3 arc-second grids (about 90 m) over
all land from 90N to 60S, in 5-degree tiles: a D8 flow direction per cell, the
upstream drainage area (km2) and a channel width (m). The network is traced
from those in two passes, so the expensive one is parallel:

  extract    per tile: every cell draining at least MIN_UPA km2, with the
             global index of the cell it flows into. A tile needs nothing
             from its neighbours -- the downstream index is arithmetic on the
             cell's own direction -- so tiles run in any order, anywhere.
  assemble   all tiles at once: join the cells into one network, compute the
             Strahler order, cut it into stretches between confluences, and
             write lines.

MIN_UPA is 10 km2 because that is where HydroRIVERS starts a river too: the
Strahler orders come out comparable, so the zoom rules written for HydroRIVERS
mean roughly the same thing here. What MERIT does not have is discharge; the
drainage area and the width stand in for it (see docs/pipeline/merit-rivers.md).

Everything is numpy. The graph work -- a topological pass for the order and
pointer jumping for the stretches -- is vectorised by level, so it needs no
compiler on the cluster and no new wheels in its offline wheelhouse.
"""
from __future__ import annotations

import os
import re
import tarfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rich.console import Console

from basemap.config import get_data_dir, get_work_dir
from basemap.utils.paths import ensure_dir

console = Console()

ATTRIBUTION = ("Contains information from MERIT Hydro (Yamazaki et al. 2019, "
               "doi:10.1029/2019WR024873), made available under the ODbL 1.0")

# The global 3 arc-second grid MERIT is cut from. Its cell *centres* sit on
# whole multiples of 3 seconds -- a tile's name is the centre of its lower-left
# cell, and its GeoTIFF origin is half a cell outside the round number (tile
# n55w155 starts at -155.000417, 59.999583). So column c is centred on
# -180 + c * CELL_DEG, 360 degrees of them, and row r on 90 - (r + 1) * CELL_DEG,
# from just below the pole down to 60S.
CELL_DEG = 3.0 / 3600.0
NCOLS = 432_000
NROWS = 180_000

MIN_UPA = float(os.environ.get("BASEMAP_MERIT_MIN_UPA") or 10.0)

# D8 as MERIT stores it, in an int8 grid read as uint8: the eight neighbours
# by power of two, 0 a river mouth, 255 (-1) an inland pit, 247 (-9) no data.
# 128 does not fit in an int8 and arrives as -128, which uint8 reads as 128.
_D8 = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
       16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}
NODATA = 247
DROW = np.zeros(256, np.int64)
DCOL = np.zeros(256, np.int64)
FLOWS = np.zeros(256, bool)
for _code, (_dr, _dc) in _D8.items():
    DROW[_code], DCOL[_code], FLOWS[_code] = _dr, _dc, True


@dataclass
class Cells:
    """River cells of one or more tiles: where each one flows, and what it carries."""

    idx: np.ndarray      # int64, global cell index (row * NCOLS + col)
    ds: np.ndarray       # int64, the cell it flows into, or -1 (mouth, pit, edge)
    upa: np.ndarray      # float32, upstream drainage area, km2
    wth: np.ndarray      # float32, channel width, m (0 where MERIT has none)

    @classmethod
    def concat(cls, parts: list["Cells"]) -> "Cells":
        return cls(*(np.concatenate([getattr(p, f) for p in parts]) if parts
                     else np.empty(0, t)
                     for f, t in (("idx", np.int64), ("ds", np.int64),
                                  ("upa", np.float32), ("wth", np.float32))))

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".part.npz")
        np.savez_compressed(tmp, idx=self.idx, ds=self.ds, upa=self.upa, wth=self.wth)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Cells":
        with np.load(path) as z:
            return cls(z["idx"], z["ds"], z["upa"], z["wth"])


def tile_transform(row: int, col: int):
    """The GeoTIFF transform MERIT gives a tile whose top-left cell is (row, col)."""
    from rasterio.transform import from_origin

    west = -180.0 + col * CELL_DEG - CELL_DEG / 2
    north = 90.0 - (row + 1) * CELL_DEG + CELL_DEG / 2
    return from_origin(west, north, CELL_DEG, CELL_DEG)


def grid_offset(transform) -> tuple[int, int]:
    """(row, col) of a tile's top-left cell in the global grid.

    Checked rather than assumed: a tile off the 3-second lattice would put
    every river it holds a fraction of a cell out, and nothing downstream
    would notice.
    """
    col = (transform.c + CELL_DEG / 2 + 180.0) / CELL_DEG
    row = (90.0 - (transform.f - CELL_DEG / 2)) / CELL_DEG - 1
    if (abs(transform.a - CELL_DEG) > 1e-9 or abs(transform.e + CELL_DEG) > 1e-9
            or abs(col - round(col)) > 1e-3 or abs(row - round(row)) > 1e-3):
        raise ValueError(f"not on the MERIT 3-second grid: {transform}")
    return int(round(row)), int(round(col))


def extract(dir_path: str, upa_path: str, wth_path: str | None = None,
            min_upa: float = MIN_UPA) -> Cells:
    """The river cells of one tile and the global index each one flows into."""
    import rasterio

    with rasterio.open(dir_path) as src:
        row0, col0 = grid_offset(src.transform)
        dirs = src.read(1).view(np.uint8) if src.dtypes[0] == "int8" else src.read(1).astype(np.uint8)
    with rasterio.open(upa_path) as src:
        if grid_offset(src.transform) != (row0, col0):
            raise ValueError(f"{upa_path} is not the same tile as {dir_path}")
        upa = src.read(1)
    rows, cols = np.nonzero((upa >= min_upa) & (dirs != NODATA))
    code = dirs[rows, cols]
    grow = rows.astype(np.int64) + row0
    gcol = cols.astype(np.int64) + col0
    ds_row = grow + DROW[code]
    # A river can cross the antimeridian; the grid wraps there.
    ds_col = (gcol + DCOL[code]) % NCOLS
    flows = FLOWS[code] & (ds_row >= 0) & (ds_row < NROWS)
    ds = np.where(flows, ds_row * NCOLS + ds_col, -1)
    if wth_path:
        with rasterio.open(wth_path) as src:
            wth = src.read(1)[rows, cols].astype(np.float32)
        wth = np.where(np.isfinite(wth) & (wth > 0), wth, 0).astype(np.float32)
    else:
        wth = np.zeros(rows.size, np.float32)
    return Cells(grow * NCOLS + gcol, ds, upa[rows, cols].astype(np.float32), wth)


@dataclass
class Network:
    """The joined network: every river cell, its order, and its stretch."""

    idx: np.ndarray      # int64, sorted global cell index
    ds: np.ndarray       # int64, position of the downstream cell in idx, or -1
    upa: np.ndarray
    wth: np.ndarray
    order: np.ndarray    # uint8 Strahler order; 0 for a cell left out of a cycle
    rank: np.ndarray     # int64 topological rank, upstream before downstream
    head: np.ndarray     # int64 position of the first cell of the cell's stretch


def assemble(cells: Cells) -> Network:
    """Join the cells, then order and cut the network."""
    sort = np.argsort(cells.idx, kind="stable")
    idx = cells.idx[sort]
    if idx.size and np.any(idx[1:] == idx[:-1]):
        # Two tiles claiming one cell would double the river through it.
        keep = np.r_[True, idx[1:] != idx[:-1]]
        sort, idx = sort[keep], idx[keep]
    ds_idx, upa, wth = cells.ds[sort], cells.upa[sort], cells.wth[sort]
    pos = np.searchsorted(idx, ds_idx)
    pos_c = np.minimum(pos, max(idx.size - 1, 0))
    found = (ds_idx >= 0) & (pos < idx.size) & (idx[pos_c] == ds_idx)
    ds = np.where(found, pos, -1)
    order, rank = strahler(ds)
    head = stretch_heads(ds, order)
    return Network(idx, ds, upa, wth, order, rank, head)


def strahler(ds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Strahler order and a topological rank for every cell.

    Kahn's algorithm, one level at a time: the cells with nothing left
    upstream are finished together, then hand their order to the cells they
    flow into. A cell's order is the highest arriving, plus one when at least
    two arrivals share it; a source is 1. The number of levels is the longest
    river in cells, so the loop is long but each pass is a handful of array
    operations on the cells that just finished.
    """
    n = ds.size
    upcount = np.bincount(ds[ds >= 0], minlength=n)
    remaining = upcount.astype(np.int64)
    order = np.zeros(n, np.uint8)
    best = np.zeros(n, np.uint8)
    ties = np.zeros(n, np.int64)
    rank = np.full(n, -1, np.int64)
    frontier = np.flatnonzero(remaining == 0)
    done = 0
    while frontier.size:
        b = best[frontier]
        o = np.where(b == 0, 1, b + (ties[frontier] >= 2)).astype(np.uint8)
        order[frontier] = o
        rank[frontier] = np.arange(done, done + frontier.size)
        done += frontier.size
        d = ds[frontier]
        keep = d >= 0
        d, o = d[keep], o[keep]
        if not d.size:
            break
        s = np.lexsort((o, d))
        d, o = d[s], o[s]
        starts = np.r_[0, np.flatnonzero(d[1:] != d[:-1]) + 1]
        sizes = np.diff(np.r_[starts, d.size])
        recv = d[starts]
        top = o[starts + sizes - 1]
        count = np.add.reduceat((o == np.repeat(top, sizes)).astype(np.int64), starts)
        cur_b, cur_t = best[recv], ties[recv]
        higher, equal = top > cur_b, top == cur_b
        best[recv] = np.where(higher, top, cur_b)
        ties[recv] = np.where(higher, count, np.where(equal, cur_t + count, cur_t))
        remaining[recv] -= sizes
        frontier = recv[remaining[recv] == 0]
    if done < n:
        # Only a cycle leaves cells unfinished. D8 from a conditioned DEM has
        # none, so say so rather than guess an order for them.
        console.print(f"  [yellow]{n - done:,} cells sit in flow cycles; left out.[/yellow]")
    return order, rank


def stretch_heads(ds: np.ndarray, order: np.ndarray) -> np.ndarray:
    """For every cell, the position of the first cell of its stretch.

    A stretch starts at a source or a confluence -- any cell that does not
    have exactly one cell flowing into it -- and runs down to the next one.
    Each cell with one inflow points at that inflow, and pointer jumping
    collapses the chains in a few dozen passes, however long the river.
    """
    n = ds.size
    upcount = np.bincount(ds[ds >= 0], minlength=n)
    up = np.full(n, -1, np.int64)
    flows = np.flatnonzero(ds >= 0)
    single = flows[upcount[ds[flows]] == 1]
    up[ds[single]] = single
    starts = (upcount != 1) | (order == 0)
    head = np.where(starts | (up < 0), np.arange(n), up)
    while True:
        nxt = head[head]
        if np.array_equal(nxt, head):
            return head
        head = nxt


def cell_centres(idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    row, col = np.divmod(idx, NCOLS)
    return -180.0 + col * CELL_DEG, 90.0 - (row + 1) * CELL_DEG


@dataclass
class Lines:
    """Stretches as vertex runs: x, y, and the stretch each vertex belongs to."""

    x: np.ndarray
    y: np.ndarray
    part: np.ndarray        # int64, stretch number per vertex, ascending
    order: np.ndarray       # per stretch
    upa: np.ndarray         # per stretch, at its downstream end, km2
    width: np.ndarray       # per stretch, mean of MERIT's widths where it has one, m
    wet: np.ndarray         # per stretch, share of its cells with a width


def stretches(net: Network) -> Lines:
    """Each stretch as a run of cell centres, ending on the cell it flows into.

    The last vertex is the next stretch's first cell, so tributaries meet the
    river they join instead of stopping a cell short. A stretch that crosses
    the antimeridian is split there rather than drawn the long way round.
    """
    live = np.flatnonzero(net.order > 0)
    key = np.lexsort((net.rank[live], net.head[live]))
    cells = live[key]
    head = net.head[cells]
    last = np.r_[head[1:] != head[:-1], True]
    ends = cells[last]
    tail = net.ds[ends]
    # Insert each stretch's downstream cell after its last cell.
    at = np.flatnonzero(last) + 1
    with_tail = tail >= 0
    seq = np.insert(cells, at[with_tail], tail[with_tail])
    group = np.insert(head, at[with_tail], head[last][with_tail])
    is_tail = np.zeros(seq.size, bool)
    is_tail[at[with_tail] + np.arange(with_tail.sum())] = True
    x, y = cell_centres(net.idx[seq])
    # New part at every new stretch and wherever the line jumps the antimeridian.
    new = np.r_[True, group[1:] != group[:-1]] | np.r_[False, np.abs(np.diff(x)) > 180.0]
    part = np.cumsum(new) - 1
    # Per-part attributes from the stretch's own cells, not the borrowed tail.
    own = ~is_tail
    nparts = int(part[-1]) + 1 if part.size else 0
    count = np.bincount(part[own], minlength=nparts)
    wet_n = np.bincount(part[own], weights=(net.wth[seq[own]] > 0), minlength=nparts)
    wsum = np.bincount(part[own], weights=net.wth[seq[own]], minlength=nparts)
    # The downstream end of each part: its last own vertex. Taken as a maximum
    # rather than by repeated assignment, whose winner numpy does not promise.
    own_pos = np.flatnonzero(own)
    last_pos = np.zeros(nparts, np.int64)
    np.maximum.at(last_pos, part[own_pos], own_pos)
    last_own = seq[last_pos]
    with np.errstate(invalid="ignore", divide="ignore"):
        width = np.where(wet_n > 0, wsum / np.maximum(wet_n, 1), 0.0)
        wet = np.where(count > 0, wet_n / np.maximum(count, 1), 0.0)
    lines = Lines(x, y, part, net.order[last_own], net.upa[last_own].astype(np.float64),
                  width, wet)
    return drop_points(lines)


def drop_points(lines: Lines) -> Lines:
    """Parts of one vertex, which a split can leave, are not lines."""
    n = np.bincount(lines.part)
    keep_part = n >= 2
    if keep_part.all():
        return lines
    keep_v = keep_part[lines.part]
    renum = np.cumsum(keep_part) - 1
    return Lines(lines.x[keep_v], lines.y[keep_v], renum[lines.part[keep_v]],
                 lines.order[keep_part], lines.upa[keep_part],
                 lines.width[keep_part], lines.wet[keep_part])


def smooth(lines: Lines, passes: int = 1) -> Lines:
    """Round off the D8 stair-steps without moving the ends of a stretch.

    Each interior vertex moves to a quarter of each neighbour and half of
    itself. The ends stay put, so stretches still meet at their confluences.
    """
    x, y = lines.x.copy(), lines.y.copy()
    first = np.r_[True, lines.part[1:] != lines.part[:-1]]
    lastv = np.r_[lines.part[1:] != lines.part[:-1], True]
    interior = ~(first | lastv)
    for _ in range(passes):
        xp, xn = np.r_[x[:1], x[:-1]], np.r_[x[1:], x[-1:]]
        yp, yn = np.r_[y[:1], y[:-1]], np.r_[y[1:], y[-1:]]
        x = np.where(interior, 0.25 * xp + 0.5 * x + 0.25 * xn, x)
        y = np.where(interior, 0.25 * yp + 0.5 * y + 0.25 * yn, y)
    return Lines(x, y, lines.part, lines.order, lines.upa, lines.width, lines.wet)


def geometries(lines: Lines, tolerance: float = 0.5 * CELL_DEG) -> np.ndarray:
    """Shapely LineStrings, simplified to half a cell: D8 runs collapse to two points."""
    import shapely

    geoms = shapely.linestrings(np.column_stack([lines.x, lines.y]), indices=lines.part)
    return shapely.simplify(geoms, tolerance, preserve_topology=False)


SCHEMA = {"geometry": "LineString",
          "properties": {"order": "int", "upa": "float", "width": "float", "wet": "float"}}


def write(path: Path, lines: Lines, geoms: np.ndarray) -> int:
    """The network as one FlatGeobuf with a spatial index; returns the count."""
    import fiona
    import shapely

    tmp = path.with_name(path.name + ".part.fgb")
    tmp.unlink(missing_ok=True)
    written = 0
    with fiona.open(tmp, "w", driver="FlatGeobuf", schema=SCHEMA, crs="EPSG:4326") as dst:
        batch = []
        for i, geom in enumerate(geoms):
            if geom is None or shapely.is_empty(geom):
                continue
            batch.append({"geometry": shapely.geometry.mapping(geom),
                          "properties": {"order": int(lines.order[i]),
                                         "upa": round(float(lines.upa[i]), 2),
                                         "width": round(float(lines.width[i]), 1),
                                         "wet": round(float(lines.wet[i]), 3)}})
            if len(batch) >= 50_000:
                dst.writerecords(batch)
                written += len(batch)
                batch = []
        if batch:
            dst.writerecords(batch)
            written += len(batch)
    tmp.replace(path)
    return written


# ---------------------------------------------------------------------------
# Inputs and the step
# ---------------------------------------------------------------------------

_TILE = re.compile(r"(?P<tile>[ns]\d{2}[ew]\d{3})_(?P<var>dir|upa|wth)\.tif$")


def merit_dir(work: Path) -> Path:
    """Where the MERIT packages are staged; BASEMAP_MERIT_DIR overrides it."""
    explicit = os.environ.get("BASEMAP_MERIT_DIR")
    return Path(explicit) if explicit else work / "merit_hydro"


def tile_sets(src: Path) -> dict[str, dict[str, str]]:
    """GDAL paths of each tile's grids, from MERIT's tar packages or loose GeoTIFFs.

    Read in place through /vsitar/: the packages are 30 by 30 degrees, and
    unpacking them would double what scratch holds for no gain.
    """
    tiles: dict[str, dict[str, str]] = {}
    for tif in sorted(src.rglob("*.tif")):
        m = _TILE.search(tif.name)
        if m:
            tiles.setdefault(m["tile"], {})[m["var"]] = str(tif)
    for tar in sorted(src.rglob("*.tar")):
        with tarfile.open(tar) as tf:
            for member in tf.getnames():
                m = _TILE.search(member)
                if m:
                    tiles.setdefault(m["tile"], {}).setdefault(m["var"], f"/vsitar/{tar}/{member}")
    return tiles


def _extract_one(args) -> tuple[str, int]:
    tile, paths, out, min_upa = args
    if out.is_file():
        return tile, -1
    cells = extract(paths["dir"], paths["upa"], paths.get("wth"), min_upa)
    cells.save(out)
    return tile, cells.idx.size


def network_fgb(work: Path) -> Path:
    return work / "land" / "merit_rivers.fgb"


def run(data_dir: Path | None = None) -> None:
    from rich.progress import Progress

    from basemap.config import RENDER_WORKERS

    console.print("\n[bold]Step 05h: River network from MERIT Hydro[/bold]\n")
    work = get_work_dir(get_data_dir(data_dir))
    out = network_fgb(work)
    if out.is_file():
        console.print(f"  {out.name} exists; delete it to trace again.")
        return
    src = merit_dir(work)
    tiles = {t: p for t, p in tile_sets(src).items() if {"dir", "upa"} <= p.keys()}
    if not tiles:
        raise SystemExit(f"no MERIT Hydro tiles under {src}; stage the dir and upa packages")
    without_width = sum("wth" not in p for p in tiles.values())
    console.print(f"  {len(tiles):,} tiles under {src}, cells draining >= {MIN_UPA:g} km2"
                  + (f"; {without_width:,} without a width grid" if without_width else ""))

    console.print("=== Step 1: River cells, per tile ===")
    cell_dir = ensure_dir(work / "merit_rivers" / f"cells_{MIN_UPA:g}")
    jobs = [(t, p, cell_dir / f"{t}.npz", MIN_UPA) for t, p in sorted(tiles.items())]
    with Progress(console=console) as progress, ProcessPoolExecutor(RENDER_WORKERS) as pool:
        task = progress.add_task("Tiles", total=len(jobs))
        for _ in pool.map(_extract_one, jobs, chunksize=1):
            progress.advance(task)

    console.print("=== Step 2: Join, order and cut ===")
    cells = Cells.concat([Cells.load(cell_dir / f"{t}.npz") for t in sorted(tiles)])
    console.print(f"  {cells.idx.size:,} river cells")
    net = assemble(cells)
    del cells
    console.print(f"  highest Strahler order {int(net.order.max()) if net.order.size else 0}")
    lines = smooth(stretches(net))
    del net
    console.print(f"  {lines.order.size:,} stretches between confluences")

    console.print("=== Step 3: Write ===")
    ensure_dir(out.parent)
    written = write(out, lines, geometries(lines))
    console.print(f"[green]Done:[/green] {written:,} stretches in {out}")
