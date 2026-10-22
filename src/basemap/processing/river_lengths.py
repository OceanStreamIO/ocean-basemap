"""How much river a selection rule draws, for comparing one network with another.

The zoom rules in pipeline/rivers.py were written for HydroRIVERS: a reach is
drawn at a zoom if its Strahler order or its mean discharge clears that zoom's
bar. MERIT carries the same two fields, traced from a different DEM and with
its discharge estimated from a different runoff, so the same numbers need not
draw the same map.

This measures a network once -- kilometres of line by order, by discharge
band and by 10-degree cell -- after which any rule's total is a sum over a
small array, worldwide or region by region. Measured on 2026-10-22, HydroRIVERS'
own rule drew 109-121 % of HydroRIVERS' length from MERIT worldwide, and
93-138 % in every region but Australia: the orders agree at the scale of a
continent, and differ locally, on wet coasts above all, which is what the
discharge bar is for.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rich.console import Console

from basemap.pipeline.rivers import DRY_DIS

console = Console()

# Discharge band edges, m3/s. A rule is measured exactly when its bars are among these.
DIS_EDGES = np.array([0.0, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500,
                      1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, np.inf])
MAX_ORDER = 12
CELL_DEG = 10
NX, NY = 360 // CELL_DEG, 180 // CELL_DEG
KM_PER_DEG = 111.32


@dataclass
class Lengths:
    """Kilometres of line per (cell, order, discharge band)."""

    km: np.ndarray       # float64 (NX * NY, MAX_ORDER + 1, len(DIS_EDGES) - 1)
    source: str = ""

    def save(self, path: Path) -> None:
        np.savez_compressed(path, km=self.km, dis_edges=DIS_EDGES, cell_deg=CELL_DEG,
                            source=self.source)

    @classmethod
    def load(cls, path: Path) -> Lengths:
        with np.load(path) as z:
            if not np.array_equal(z["dis_edges"], DIS_EDGES) or int(z["cell_deg"]) != CELL_DEG:
                raise ValueError(f"{path} was measured with other bands; measure it again")
            return cls(z["km"], str(z["source"]))

    def drawn(self, order: int, dis: float, cells: np.ndarray | None = None,
              dry: bool | None = None) -> float:
        """Km drawn by 'order >= *order* or discharge >= *dis*'.

        *dry* True or False counts only the channels below or above DRY_DIS;
        *cells* limits the sum to those cell indices.
        """
        lo = DIS_EDGES[:-1]
        mask = (np.arange(MAX_ORDER + 1)[:, None] >= order) | (lo[None, :] >= dis)
        if dry is not None:
            band_dry = DIS_EDGES[1:] <= DRY_DIS
            mask &= (band_dry if dry else ~band_dry)[None, :]
        km = self.km if cells is None else self.km[cells]
        return float(km[:, mask].sum())


def cell_of(lon: float, lat: float) -> int:
    col = min(max(int((lon + 180.0) // CELL_DEG), 0), NX - 1)
    row = min(max(int((90.0 - lat) // CELL_DEG), 0), NY - 1)
    return row * NX + col


def cells_in(west: float, south: float, east: float, north: float) -> np.ndarray:
    """The cells whose centres fall inside a box."""
    col = np.arange(NX)
    row = np.arange(NY)
    lon = -180.0 + (col + 0.5) * CELL_DEG
    lat = 90.0 - (row + 0.5) * CELL_DEG
    cs = col[(lon >= west) & (lon <= east)]
    rs = row[(lat >= south) & (lat <= north)]
    return (rs[:, None] * NX + cs[None, :]).ravel()


def line_km(xy: np.ndarray) -> float:
    """Length of a lon/lat line, km, flat-earth per segment: ample for totals."""
    if len(xy) < 2:
        return 0.0
    d = np.diff(xy, axis=0)
    mid = 0.5 * (xy[1:, 1] + xy[:-1, 1])
    return float(np.hypot(d[:, 0] * np.cos(np.radians(mid)), d[:, 1]).sum() * KM_PER_DEG)


def measure(path: str | Path) -> Lengths:
    """Kilometres of each line in a network with 'order' and 'dis' fields."""
    import fiona

    km = np.zeros((NX * NY, MAX_ORDER + 1, len(DIS_EDGES) - 1))
    n = 0
    with fiona.open(path) as src:
        for f in src:
            g = f["geometry"]
            if g is None:
                continue
            parts = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            p = f["properties"]
            order = min(max(int(p["order"] or 0), 0), MAX_ORDER)
            band = int(np.searchsorted(DIS_EDGES, float(p["dis"] or 0.0), side="right")) - 1
            for part in parts:
                xy = np.asarray(part, dtype=np.float64)[:, :2]
                if len(xy) < 2:
                    continue
                mx, my = xy[len(xy) // 2]
                km[cell_of(mx, my), order, band] += line_km(xy)
            n += 1
            if n % 1_000_000 == 0:
                console.print(f"  {n:,} lines")
    console.print(f"  {n:,} lines, {km.sum():,.0f} km")
    return Lengths(km, str(path))


@dataclass
class Row:
    zoom: int
    hydro_rule: tuple[int, float]
    merit_rule: tuple[int, float]
    hydro_km: float
    merit_km: float
    merit_dry_km: float
    regions: dict[str, float]       # MERIT km as a share of HydroRIVERS', per region


def compare(merit: Lengths, hydro: Lengths, hydro_selection, merit_selection) -> list[Row]:
    """Per zoom band, how much river each rule draws, worldwide and by region.

    Both selections are (up to zoom, least order, least discharge) rows, as in
    pipeline/rivers.py, and must name the same zooms.
    """
    rows = []
    for (zoom, ho, hd), (mz, mo, md) in zip(hydro_selection, merit_selection, strict=True):
        if zoom != mz:
            raise ValueError(f"zoom bands differ: {zoom} and {mz}")
        regions = {}
        for name, box in REGIONS.items():
            cells = cells_in(*box)
            h = hydro.drawn(ho, hd, cells)
            regions[name] = merit.drawn(mo, md, cells) / h if h > 0 else float("nan")
        rows.append(Row(zoom, (ho, hd), (mo, md), hydro.drawn(ho, hd), merit.drawn(mo, md),
                        merit.drawn(mo, md, dry=True), regions))
    return rows


# Where the two networks were seen to differ, plus the big basins, for the report.
REGIONS = {
    "Europe": (-10, 35, 40, 70),
    "Sahara": (-15, 15, 35, 35),
    "Amazon": (-80, -20, -40, 10),
    "Congo": (10, -15, 35, 10),
    "South Asia": (60, 5, 100, 35),
    "East Asia": (100, 20, 145, 55),
    "Siberia": (60, 50, 180, 80),
    "North America": (-170, 25, -50, 75),
    "Australia": (110, -45, 155, -10),
}


def report(rows: list[Row]) -> None:
    from rich.table import Table

    t = Table(title="River drawn per zoom band: MERIT as a share of HydroRIVERS")
    for c in ("up to z", "HydroRIVERS", "MERIT", "world", "dry km", *REGIONS):
        t.add_column(c, justify="right")
    for r in rows:
        t.add_row(str(r.zoom), f"{r.hydro_rule[0]} or {r.hydro_rule[1]:g}",
                  f"{r.merit_rule[0]} or {r.merit_rule[1]:g}",
                  f"{r.merit_km / r.hydro_km:.0%}", f"{r.merit_dry_km:,.0f}",
                  *(f"{s:.0%}" if s == s else "-" for s in r.regions.values()))
    console.print(t)
