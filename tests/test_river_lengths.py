"""Measuring river networks and comparing their zoom rules."""
from __future__ import annotations

import numpy as np
import pytest

from basemap.processing import river_lengths as rl


def _write(path, lines):
    import fiona

    schema = {"geometry": "LineString", "properties": {"order": "int", "dis": "float"}}
    with fiona.open(path, "w", driver="FlatGeobuf", schema=schema, crs="EPSG:4326") as dst:
        for coords, order, dis in lines:
            dst.write({"geometry": {"type": "LineString", "coordinates": coords},
                       "properties": {"order": order, "dis": dis}})
    return path


def _band(dis):
    return int(np.searchsorted(rl.DIS_EDGES, dis, side="right")) - 1


def test_a_degree_of_latitude_is_about_111_km():
    assert rl.line_km(np.array([[10.0, 50.0], [10.0, 51.0]])) == pytest.approx(111.32)
    # A degree of longitude shrinks with the cosine of the latitude.
    assert rl.line_km(np.array([[0.0, 60.0], [1.0, 60.0]])) == pytest.approx(55.66, abs=0.01)


def test_lines_land_in_their_cell_order_and_band(tmp_path):
    fgb = _write(tmp_path / "net.fgb", [
        ([(5.0, 52.0), (5.0, 53.0)], 6, 2300.0),       # the Rhine, near enough
        ([(30.0, 25.0), (30.0, 25.5)], 4, 0.2),        # a wadi
    ])
    m = rl.measure(fgb)
    assert m.km.sum() == pytest.approx(111.32 * 1.5)
    assert m.km[rl.cell_of(5.0, 52.5), 6, _band(2300.0)] == pytest.approx(111.32)
    assert m.km[rl.cell_of(30.0, 25.25), 4, _band(0.2)] == pytest.approx(55.66)


def test_a_rule_draws_by_order_or_discharge_and_splits_dry_from_wet():
    km = np.zeros((rl.NX * rl.NY, rl.MAX_ORDER + 1, len(rl.DIS_EDGES) - 1))
    km[0, 5, _band(0.2)] = 10.0      # dry, order 5
    km[0, 2, _band(30.0)] = 20.0     # wet, order 2, 30 m3/s
    km[0, 2, _band(3.0)] = 40.0      # wet, order 2, 3 m3/s
    m = rl.Lengths(km)
    assert m.drawn(4, 20.0) == 30.0                  # the dry order-5 and the 30 m3/s
    assert m.drawn(4, 20.0, dry=True) == 10.0
    assert m.drawn(4, 20.0, dry=False) == 20.0
    assert m.drawn(6, 2.0) == 60.0                   # both wet ones, no dry
    assert m.drawn(4, 20.0, cells=np.array([1])) == 0.0


def test_the_comparison_measures_each_rule_on_its_own_network():
    shape = (rl.NX * rl.NY, rl.MAX_ORDER + 1, len(rl.DIS_EDGES) - 1)
    hydro, merit = np.zeros(shape), np.zeros(shape)
    europe = rl.cell_of(5.0, 52.0)
    hydro[europe, 4, _band(30.0)] = 100.0     # drawn by both of its rules
    merit[europe, 2, _band(15.0)] = 60.0      # drawn only by the lower discharge bar
    merit[europe, 4, _band(0.1)] = 60.0       # a dry channel, drawn for its order
    (row,) = rl.compare(rl.Lengths(merit), rl.Lengths(hydro), [(10, 4, 20.0)], [(10, 4, 10.0)])
    assert (row.hydro_km, row.merit_km, row.merit_dry_km) == (100.0, 120.0, 60.0)
    assert row.regions["Europe"] == pytest.approx(1.2)
    assert np.isnan(row.regions["Sahara"])
    with pytest.raises(ValueError):
        rl.compare(rl.Lengths(merit), rl.Lengths(hydro), [(10, 4, 20.0)], [(9, 4, 10.0)])


def test_lengths_measured_with_other_bands_are_refused(tmp_path):
    path = tmp_path / "old.npz"
    np.savez(path, km=np.zeros(1), dis_edges=np.array([0.0, 1.0]), cell_deg=10, source="")
    with pytest.raises(ValueError):
        rl.Lengths.load(path)


def test_cells_in_a_box():
    # Europe: centres 5W to 35E and 35N to 65N, five columns by four rows.
    cells = rl.cells_in(-10, 35, 40, 70)
    assert cells.size == 20
    assert rl.cell_of(5.0, 52.0) in cells
