"""How much river a selection rule draws, for calibrating one network against another.

The zoom rules in pipeline/rivers.py were written for HydroRIVERS: a reach is
drawn at a zoom if its Strahler order or its mean discharge clears that zoom's
bar. The MERIT network carries the same two fields, but its orders run lower
than HydroRIVERS' on wet coasts and higher in deserts, and its discharge is an
estimate from a different runoff, so the same numbers draw a different map.

This measures both networks once -- kilometres of line by order, by discharge
band and by 10-degree cell -- after which any rule's total is a sum over a
small array, and the thresholds for MERIT can be fitted so each zoom draws as
much river as HydroRIVERS did:

  dry channels  (discharge under DRY_DIS) are only ever drawn for their order,
                so the order threshold is fitted on them alone: the deserts
                stay as dense as they are now.
  wet rivers    then get the lowest discharge threshold that, with that order,
                draws no more river than HydroRIVERS did.

The cells let a fit be checked region by region, which is where the two
networks differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rich.console import Console

console = Console()

# Discharge band edges, m3/s. A fitted threshold is always one of these.
DIS_EDGES = np.array([0.0, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500,
                      1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, np.inf])
# Below this a channel is dry for the map: drawn as intermittent, if at all.
DRY_DIS = 0.5
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
class Fit:
    zoom: int
    order: int           # HydroRIVERS' rule at this zoom
    dis: float
    merit_order: int     # the fitted rule
    merit_dis: float
    hydro_dry_km: float
    hydro_wet_km: float
    merit_dry_km: float
    merit_wet_km: float


def fit(merit: Lengths, hydro: Lengths, selection, orders=range(2, MAX_ORDER + 1)) -> list[Fit]:
    """Per zoom, the MERIT rule that draws about as much as HydroRIVERS' did.

    *selection* is rivers.SELECTION: (up to zoom, least order, least discharge).
    The order is the one whose dry channels come closest to HydroRIVERS' dry
    total; the discharge the lowest band edge that keeps the wet total at or
    under HydroRIVERS'. Both are on a log scale, so too much and too little of
    the same factor count alike.
    """
    out = []
    for zoom, order, dis in selection:
        h_dry = hydro.drawn(order, dis, dry=True)
        h_wet = hydro.drawn(order, dis, dry=False)

        def miss(km: float, target: float) -> float:
            return abs(np.log((km + 1.0) / (target + 1.0)))

        m_order = min(orders, key=lambda o: miss(merit.drawn(o, np.inf, dry=True), h_dry))
        edges = [e for e in DIS_EDGES[1:-1] if e >= DRY_DIS]
        under = [e for e in edges if merit.drawn(m_order, e, dry=False) <= h_wet]
        m_dis = float(min(under)) if under else float(edges[-1])
        out.append(Fit(zoom, order, dis, m_order, m_dis, h_dry, h_wet,
                       merit.drawn(m_order, np.inf, dry=True),
                       merit.drawn(m_order, m_dis, dry=False)))
    return out


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


def report(merit: Lengths, hydro: Lengths, fits: list[Fit]) -> None:
    from rich.table import Table

    t = Table(title="Fitted MERIT rule per zoom (km drawn, world)")
    for c in ("up to z", "HydroRIVERS rule", "MERIT rule", "dry km H / M", "wet km H / M"):
        t.add_column(c, justify="right")
    for f in fits:
        t.add_row(str(f.zoom), f"order {f.order} or {f.dis:g} m3/s",
                  f"order {f.merit_order} or {f.merit_dis:g} m3/s",
                  f"{f.hydro_dry_km:,.0f} / {f.merit_dry_km:,.0f}",
                  f"{f.hydro_wet_km:,.0f} / {f.merit_wet_km:,.0f}")
    console.print(t)
    for f in (fits[0], fits[-1]):
        r = Table(title=f"Regions at z{f.zoom}: MERIT km as a share of HydroRIVERS'")
        r.add_column("region")
        r.add_column("dry", justify="right")
        r.add_column("wet", justify="right")
        for name, box in REGIONS.items():
            cells = cells_in(*box)
            share = []
            for dry in (True, False):
                h = hydro.drawn(f.order, f.dis, cells, dry)
                m = merit.drawn(f.merit_order, f.merit_dis, cells, dry)
                share.append(f"{m / h:.0%}" if h > 0 else "-")
            r.add_row(name, *share)
        console.print(r)
