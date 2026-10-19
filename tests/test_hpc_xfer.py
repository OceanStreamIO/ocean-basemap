"""The SSH transfer helper: planning, filters, listings, and that nothing on
the cluster is asked to connect out."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HPC = ROOT / "deploy" / "hpc"


def _xfer():
    spec = importlib.util.spec_from_file_location("xfer", HPC / "xfer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["xfer"] = module          # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


xfer = _xfer()


def test_only_what_is_missing_or_a_different_size_moves():
    source = {"a.tif": 10, "b.tif": 20, "sub/c.tif": 30}
    present = {"a.tif": 10, "b.tif": 19}
    items = xfer.plan(source, present, "s3://bkt/in/", "/scratch/data")
    assert [(i.source, i.dest, i.size) for i in items] == [
        ("s3://bkt/in/b.tif", "/scratch/data/b.tif", 20),
        ("s3://bkt/in/sub/c.tif", "/scratch/data/sub/c.tif", 30),
    ]
    assert len(xfer.plan(source, present, "s3://bkt/in/", "/x", always=True)) == 3


def test_flatten_drops_the_cell_directories():
    source = {"Cell_A_DEM/Cell_A_DEM.tif": 5, "Cell_B_DEM/Cell_B_DEM.tif": 6}
    items = xfer.plan(source, {"Cell_A_DEM.tif": 5}, "s3://dem/", "/scratch/glo30", flatten=True)
    assert [(i.source, i.dest) for i in items] == [
        ("s3://dem/Cell_B_DEM/Cell_B_DEM.tif", "/scratch/glo30/Cell_B_DEM.tif")]


@pytest.mark.parametrize("name, include, exclude, keep", [
    ("work/emodnet/emodnet_mean.vrt", [], ["*.vrt", "*.list"], False),
    ("work/emodnet/chunk_01.tif", [], ["*.vrt", "*.list"], True),
    ("bathy_hr.pmtiles", ["bathy_hr.pmtiles"], ["*/*"], True),
    ("v/bathy_hr-f07be8fea9c8.pmtiles", ["bathy_hr.pmtiles"], ["*/*"], False),
    ("water-a/bathy_hr.pmtiles", ["bathy_hr.pmtiles"], ["*/*"], False),
    ("versions.json", ["bathy_hr.pmtiles"], [], False),
    ("xyz.stage.000of004.mbtiles", ["xyz.stage.*.mbtiles"], [], True),
    ("xyz.stage.000of004.mbtiles-wal", ["xyz.stage.*.mbtiles"], [], False),
])
def test_filters_match_like_the_old_transfers(name, include, exclude, keep):
    assert xfer.wanted(name, include, exclude) is keep


def test_an_s3_uri_splits_into_bucket_and_key():
    assert xfer.split_s3("s3://oceanstream-bucket/basemap-build/inputs/") == (
        "oceanstream-bucket", "basemap-build/inputs/")
    with pytest.raises(SystemExit):
        xfer.split_s3("oceanstream:oceanstream-bucket/x")


def test_the_cluster_listing_is_parsed_and_partial_files_ignored(monkeypatch):
    remote = xfer.Remote("/dev/null", "user@host")
    seen = {}

    def fake_run(command, **kw):
        seen["command"] = command
        return subprocess.CompletedProcess([], 0, "a.tif\t10\nsub/b.tif\t20\n", "")

    monkeypatch.setattr(remote, "run", fake_run)
    assert remote.listing("/scratch/data") == {"a.tif": 10, "sub/b.tif": 20}
    assert "! -name '*.part'" in seen["command"], "a half-written file is not staged"

    monkeypatch.setattr(remote, "run", lambda c, **kw: subprocess.CompletedProcess([], 0, "\t42\n", ""))
    assert remote.listing("/scratch/one.json") == {"": 42}


def test_a_pull_is_renamed_only_when_whole():
    import inspect

    source = inspect.getsource(xfer.pull_one)
    assert "cat > {part}" in source and "stat -c %s {part}" in source
    assert "mv -f {part} {dest}" in source and "rm -f {part}" in source


def test_a_push_is_checked_by_size_after_the_upload():
    import inspect

    assert "store.size(item.dest)" in inspect.getsource(xfer.push_one)


def test_every_stream_gets_its_own_ssh_connection():
    import inspect

    source = inspect.getsource(xfer.Remote._args)
    assert "threading.get_ident()" in source and "ControlPath=" in source


def test_no_script_asks_the_cluster_to_connect_out():
    """The site does not allow it; transfers are opened from this side."""
    for script in sorted(HPC.glob("*.sh")) + sorted((HPC / "sbatch").glob("*")):
        text = script.read_text()
        for word in ("dtrclone", "hpc_rclone", "HPC_XFER_RCLONE_CMD", "rclone copy"):
            assert word not in text, f"{script.name} still uses {word}"
    lib = (HPC / "lib.sh").read_text()
    assert "hpc_xfer()" in lib and "xfer.py" in lib


def test_the_staged_archives_include_what_the_tree_reads():
    from basemap.pipeline import xyz

    text = (HPC / "stage-inputs.sh").read_text()
    names = {n for _, n in xyz.LAND_BY_ZOOM + xyz.SEA_BY_ZOOM} | set(xyz.LAKE_RELIEF_ARCHIVES)
    names.add(xyz.CONTOUR_ARCHIVE)
    for name in names:
        assert f"{name}.pmtiles" in text, f"stage-inputs.sh does not stage {name}"
