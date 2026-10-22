"""The river network traced from MERIT Hydro, on small synthetic tiles.

Each tile is a real GeoTIFF on the 3-second grid, so the tests exercise the
same reads, offsets and encodings the cluster run does -- only smaller.
"""
from __future__ import annotations

import tarfile

import numpy as np
import pytest

from basemap.pipeline import merit_rivers as mr

E, SE, S, SW, W, NW, N, NE = 1, 2, 4, 8, 16, 32, 64, 128
MOUTH, NODATA = 0, -9

# Somewhere in Europe: the top-left cell of the test tiles.
ROW0, COL0 = 48_000, 228_000          # 50N, 10E


def _tif(path, data, row0, col0, dtype):
    import rasterio

    with rasterio.open(path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                       count=1, dtype=dtype, crs="EPSG:4326",
                       transform=mr.tile_transform(row0, col0)) as dst:
        dst.write(np.asarray(data, dtype=dtype), 1)
    return str(path)


def _tile(tmp_path, name, dirs, upa, row0=ROW0, col0=COL0, wth=None):
    """Write one tile; dirs as MERIT codes, which int8 stores (128 as -128)."""
    d = np.where(np.asarray(dirs) == 128, -128, dirs).astype(np.int8)
    paths = {"dir": _tif(tmp_path / f"{name}_dir.tif", d, row0, col0, "int8"),
             "upa": _tif(tmp_path / f"{name}_upa.tif", upa, row0, col0, "float32")}
    if wth is not None:
        paths["wth"] = _tif(tmp_path / f"{name}_wth.tif", wth, row0, col0, "float32")
    return paths


# Two tributaries meet at (2, 2) and flow south to a mouth at (4, 2).
#    col:  0    1    2    3    4
DIRS = [[S,   NODATA, NODATA, NODATA, S],      # row 0: two sources
        [SE,  NODATA, NODATA, NODATA, SW],     # row 1
        [S,   E,      S,      W,      S],      # row 2: confluence at (2,2); (2,0),(2,4) dry
        [S,   NODATA, S,      NODATA, S],      # row 3
        [S,   NODATA, MOUTH,  NODATA, S]]      # row 4
UPA = [[10, 0, 0, 0, 10],
       [11, 0, 0, 0, 11],
       [2,  12, 25, 12, 2],
       [2,  0, 26, 0, 2],
       [2,  0, 27, 0, 2]]


def _network(tmp_path, split=False):
    dirs, upa = np.array(DIRS), np.array(UPA, dtype=np.float32)
    if not split:
        p = _tile(tmp_path, "n50e010", dirs, upa)
        return mr.assemble(mr.extract(p["dir"], p["upa"]))
    a = _tile(tmp_path, "a", dirs[:, :3], upa[:, :3])
    b = _tile(tmp_path, "b", dirs[:, 3:], upa[:, 3:], col0=COL0 + 3)
    return mr.assemble(mr.Cells.concat([mr.extract(a["dir"], a["upa"]),
                                        mr.extract(b["dir"], b["upa"])]))


def _parts(lines: mr.Lines) -> list[tuple]:
    """Each part as (order, upa, ((col, row), ...)) in grid cells, sorted."""
    col = np.round((lines.x + 180.0) / mr.CELL_DEG).astype(int) - COL0
    row = np.round((90.0 - lines.y) / mr.CELL_DEG - 1).astype(int) - ROW0
    out = []
    for p in range(lines.order.size):
        m = lines.part == p
        out.append((int(lines.order[p]), float(lines.upa[p]),
                    tuple(zip(col[m].tolist(), row[m].tolist()))))
    return sorted(out)


def test_cells_below_the_threshold_are_not_rivers(tmp_path):
    p = _tile(tmp_path, "n50e010", np.array(DIRS), np.array(UPA, dtype=np.float32))
    cells = mr.extract(p["dir"], p["upa"])
    assert cells.idx.size == 9                 # the 2 km2 and 0 km2 cells are dropped
    assert cells.upa.min() >= mr.MIN_UPA


def test_a_confluence_raises_the_order_and_cuts_the_stretch(tmp_path):
    lines = mr.stretches(_network(tmp_path))
    assert _parts(lines) == [
        (1, 12.0, ((0, 0), (0, 1), (1, 2), (2, 2))),     # west branch, ends on the confluence
        (1, 12.0, ((4, 0), (4, 1), (3, 2), (2, 2))),     # east branch, likewise
        (2, 27.0, ((2, 2), (2, 3), (2, 4))),             # the river below, to the mouth
    ]


def test_a_tile_edge_changes_nothing(tmp_path):
    (tmp_path / "whole").mkdir()
    (tmp_path / "split").mkdir()
    whole = _parts(mr.stretches(_network(tmp_path / "whole")))
    split = _parts(mr.stretches(_network(tmp_path / "split", split=True)))
    assert split == whole


def test_north_east_survives_the_int8_encoding(tmp_path):
    # (1, 0) flows north-east into (0, 1), which is a mouth.
    dirs = np.array([[NODATA, MOUTH], [NE, NODATA]])
    upa = np.array([[0, 20], [15, 0]], dtype=np.float32)
    p = _tile(tmp_path, "n50e010", dirs, upa)
    cells = mr.extract(p["dir"], p["upa"])
    src = cells.idx == (ROW0 + 1) * mr.NCOLS + COL0
    assert cells.ds[src][0] == ROW0 * mr.NCOLS + COL0 + 1


def test_a_river_across_the_antimeridian_is_followed_and_split(tmp_path):
    east = _tile(tmp_path, "e", np.array([[E, E]]), np.array([[10, 11]], dtype=np.float32),
                 row0=ROW0, col0=mr.NCOLS - 2)
    west = _tile(tmp_path, "w", np.array([[E, MOUTH]]), np.array([[12, 13]], dtype=np.float32),
                 row0=ROW0, col0=0)
    net = mr.assemble(mr.Cells.concat([mr.extract(east["dir"], east["upa"]),
                                       mr.extract(west["dir"], west["upa"])]))
    assert (net.ds >= 0).sum() == 3            # joined across the wrap
    lines = mr.stretches(net)
    assert lines.order.size == 2               # one stretch, cut at 180 degrees
    for p in range(2):
        xs = lines.x[lines.part == p]
        assert xs.max() - xs.min() < 1.0


def test_strahler_order_rules():
    # Three order-1 cells (0, 1, 3) meet at 2: order 2. Two (4, 6) meet at 7:
    # order 2. Then 2 and 7 meet at 5, two order-2 rivers: order 3.
    ds = np.array([2, 2, 5, 2, 7, -1, 7, 5])
    order, rank = mr.strahler(ds)
    assert order.tolist() == [1, 1, 2, 1, 1, 3, 1, 2]
    for cell, down in enumerate(ds):
        if down >= 0:
            assert rank[cell] < rank[down]


def test_a_flow_cycle_is_left_out_not_guessed():
    ds = np.array([1, 2, 0, 0])               # 0 -> 1 -> 2 -> 0, and 3 flows in
    order, _ = mr.strahler(ds)
    assert order.tolist() == [0, 0, 0, 1]


def test_smoothing_keeps_the_ends(tmp_path):
    lines = mr.stretches(_network(tmp_path))
    smoothed = mr.smooth(lines)
    first = np.r_[True, lines.part[1:] != lines.part[:-1]]
    last = np.r_[lines.part[1:] != lines.part[:-1], True]
    ends = first | last
    assert np.array_equal(smoothed.x[ends], lines.x[ends])
    assert np.array_equal(smoothed.y[ends], lines.y[ends])
    assert not np.array_equal(smoothed.x, lines.x)


def test_written_network_reads_back(tmp_path):
    import fiona

    lines = mr.smooth(mr.stretches(_network(tmp_path)))
    out = tmp_path / "merit_rivers.fgb"
    assert mr.write(out, lines, mr.geometries(lines)) == 3
    with fiona.open(out) as src:
        assert set(src.schema["properties"]) == {"order", "dis", "upa", "width", "wet"}
        features = list(src)
    assert sorted(f["properties"]["order"] for f in features) == [1, 1, 2]
    lon, lat = features[0]["geometry"]["coordinates"][0]
    assert 10.0 <= lon <= 10.01 and 49.99 <= lat <= 50.0
    assert mr.is_current(out)


def test_a_network_without_discharge_is_traced_again(tmp_path):
    import fiona

    old = tmp_path / "merit_rivers.fgb"
    schema = {"geometry": "LineString",
              "properties": {"order": "int", "upa": "float", "width": "float", "wet": "float"}}
    with fiona.open(old, "w", driver="FlatGeobuf", schema=schema, crs="EPSG:4326") as dst:
        dst.write({"geometry": {"type": "LineString", "coordinates": [(10, 50), (10.1, 50)]},
                   "properties": {"order": 1, "upa": 10.0, "width": 0.0, "wet": 0.0}})
    assert not mr.is_current(old)


def test_widths_and_wetness_come_from_the_stretch_itself(tmp_path):
    wth = np.zeros((5, 5), np.float32)
    wth[2, 2] = wth[3, 2] = 40.0               # the trunk, but not its mouth cell
    p = _tile(tmp_path, "n50e010", np.array(DIRS), np.array(UPA, dtype=np.float32), wth=wth)
    lines = mr.stretches(mr.assemble(mr.extract(p["dir"], p["upa"], p["wth"])))
    trunk = int(np.argmax(lines.order))
    assert lines.width[trunk] == pytest.approx(40.0)
    assert lines.wet[trunk] == pytest.approx(2 / 3)
    # The branches borrow the confluence cell as their last vertex, not its width.
    assert np.all(lines.wet[lines.order == 1] == 0)


def test_tiles_are_found_in_packages_and_read_in_place(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    p = _tile(pkg, "n50e010", np.array(DIRS), np.array(UPA, dtype=np.float32))
    tar = tmp_path / "dir_n30e000.tar"
    with tarfile.open(tar, "w") as tf:
        tf.add(p["dir"], arcname="dir_n30e000/n50e010_dir.tif")
        tf.add(p["upa"], arcname="upa_n30e000/n50e010_upa.tif")
    for f in pkg.iterdir():
        f.unlink()
    found = mr.tile_sets(tmp_path)
    assert set(found) == {"n50e010"}
    assert found["n50e010"]["dir"].startswith("/vsitar/")
    cells = mr.extract(found["n50e010"]["dir"], found["n50e010"]["upa"])
    assert cells.idx.size == 9


def test_a_tile_off_the_grid_is_refused():
    from rasterio.transform import from_origin

    with pytest.raises(ValueError):
        mr.grid_offset(from_origin(10.0001, 50.0, mr.CELL_DEG, mr.CELL_DEG))


def test_the_grid_matches_a_real_merit_tile():
    """Tile n55w155 as MERIT ships it: centres on the 3-second lattice, so the
    GeoTIFF origin is half a cell outside the round number. Its top-left cell
    is centred on 155W and one cell below 60N."""
    from rasterio.transform import from_origin

    shipped = from_origin(-155.00041666666667, 59.999583333333334, mr.CELL_DEG, mr.CELL_DEG)
    row, col = mr.grid_offset(shipped)
    lon, lat = mr.cell_centres(np.array([row * mr.NCOLS + col]))
    assert lon[0] == pytest.approx(-155.0, abs=1e-9)
    assert lat[0] == pytest.approx(60.0 - mr.CELL_DEG, abs=1e-9)
    assert mr.tile_transform(row, col).almost_equals(shipped)


# Runoff on the test tile's own 3-second cells: column c of the grid is
# column COL0 + c of MERIT's, so each tile cell can get its own value.
def _runoff(grid):
    return mr.Runoff(np.asarray(grid, np.float64), west=10.0 - mr.CELL_DEG / 2,
                     north=50.0 - mr.CELL_DEG / 2, res=mr.CELL_DEG)


def _routed(tmp_path, runoff):
    p = _tile(tmp_path, "n50e010", np.array(DIRS), np.array(UPA, dtype=np.float32))
    return mr.assemble(mr.extract(p["dir"], p["upa"]), runoff)


def test_each_river_cell_adds_only_the_land_draining_straight_into_it(tmp_path):
    net = _routed(tmp_path, None)
    local = mr.local_area(net.ds, net.upa)
    # The two sources bring their 10 km2; every cell below adds 1 km2 of slope.
    assert sorted(local.tolist()) == [1, 1, 1, 1, 1, 1, 1, 10, 10]
    assert local.sum() == pytest.approx(27.0)  # the area at the mouth, no more
    assert mr.local_area(np.array([1, -1]), np.array([10, 9.5], np.float32)).min() == 0


def test_even_runoff_brings_the_whole_basin_to_the_mouth(tmp_path):
    net = _routed(tmp_path, _runoff(np.full((5, 5), 1000.0)))
    mouth = int(np.flatnonzero(net.ds < 0)[0])
    assert net.dis[mouth] == pytest.approx(27.0 * 1000.0 * mr.KM2_MM_PER_YEAR_TO_M3S)
    assert net.dis[mouth] == pytest.approx(0.856, abs=1e-3)   # 27 km2 under 1 m a year
    flows = net.ds >= 0
    assert np.all(net.dis[net.ds[flows]] >= net.dis[flows])   # never shrinks downstream


def test_tributaries_add_up_at_the_confluence(tmp_path):
    grid = np.zeros((5, 5))
    grid[:, :3] = 1000.0                       # a wet west, a dry east
    lines = mr.stretches(_routed(tmp_path, _runoff(grid)))
    conv = 1000.0 * mr.KM2_MM_PER_YEAR_TO_M3S
    # Stretches by order, then discharge: the two branches, then the trunk.
    dis = [d for _, d in sorted(zip(lines.order.tolist(), lines.dis.tolist()))]
    assert dis[0] == pytest.approx(0.0)                    # the dry branch
    assert dis[1] == pytest.approx(12.0 * conv)            # the wet one
    assert dis[2] == pytest.approx(15.0 * conv)            # both, plus three cells of slope


def test_without_runoff_the_discharge_is_zero_and_the_order_unchanged(tmp_path):
    bare = _routed(tmp_path, None)
    wet = _routed(tmp_path, _runoff(np.full((5, 5), 500.0)))
    assert not bare.dis.any()
    assert np.array_equal(bare.order, wet.order)
    assert np.array_equal(bare.rank, wet.rank)


def test_runoff_samples_by_nearest_cell_and_reads_no_data_as_dry(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    grid = np.array([[100.0, -9999.0, 0.0, 0.0], [300.0, 0.0, 0.0, 400.0]], np.float32)
    tif = tmp_path / mr.RUNOFF_FILE
    with rasterio.open(tif, "w", driver="GTiff", height=2, width=4, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-180, 90, 90, 90),
                       nodata=-9999) as dst:
        dst.write(grid, 1)
    r = mr.Runoff.from_tif(tif)
    got = r.sample(np.array([-135.0, -45.0, -135.0, 179.99, 200.0]),
                   np.array([45.0, 45.0, -45.0, -89.9, -95.0]))
    assert got.tolist() == [100.0, 0.0, 300.0, 400.0, 400.0]   # off the edge: clamped


def test_coastal_gaps_are_filled_from_the_land_nearby():
    mean = np.zeros((1, 6))
    mean[0, 0] = 800.0
    valid = np.zeros((1, 6), bool)
    valid[0, 0] = True
    out, filled = mr.fill_coasts(mean, valid, fill_cells=2)
    assert out[0, :2].tolist() == [800.0, 800.0]           # within reach
    assert out[0, 5] == -9999.0                             # out of reach: still no data
    assert filled.tolist() == [[False, True, True, False, False, False]]
