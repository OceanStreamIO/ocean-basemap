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

    def one_file(command, **kw):
        return subprocess.CompletedProcess([], 0, "\t42\n", "")

    monkeypatch.setattr(remote, "run", one_file)
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


def test_a_stream_is_uploaded_whole_or_in_parts(monkeypatch):
    import io

    calls = []

    class Client:
        def put_object(self, **kw):
            calls.append(("put", len(kw["Body"])))

        def create_multipart_upload(self, **kw):
            calls.append(("create",))
            return {"UploadId": "u1"}

        def upload_part(self, **kw):
            calls.append(("part", kw["PartNumber"], len(kw["Body"])))
            return {"ETag": f"e{kw['PartNumber']}"}

        def complete_multipart_upload(self, **kw):
            calls.append(("complete", [p["PartNumber"] for p in kw["MultipartUpload"]["Parts"]]))

        def abort_multipart_upload(self, **kw):
            calls.append(("abort",))

    class Trickle(io.RawIOBase):
        """A pipe: hands back fewer bytes than asked for."""
        def __init__(self, data):
            self.data = data

        def read(self, n=-1):
            chunk, self.data = self.data[:min(n, 7)], self.data[min(n, 7):]
            return chunk

    store = xfer.Store.__new__(xfer.Store)
    store.client = Client()
    store.upload(Trickle(b"x" * 15), "s3://b/small", part=20)
    assert calls == [("put", 15)]
    calls.clear()
    store.upload(Trickle(b"x" * 45), "s3://b/big", part=20)
    assert calls == [("create",), ("part", 1, 20), ("part", 2, 20), ("part", 3, 5),
                     ("complete", [1, 2, 3])]


def test_replace_deletes_only_inside_the_filters():
    present = {"2023-10-10/a.nc": 1, "2023-10-10/old.nc": 1, "2023-10-11/b.nc": 1}
    source = {"2023-10-10/a.nc": 1}
    assert xfer.stale(present, source, ["2023-10-10/*"], []) == ["2023-10-10/old.nc"]


def test_the_endpoint_accepts_a_bare_host(monkeypatch):
    for name in ("XFER_S3_ENDPOINT", "BASEMAP_S3_ENDPOINT", "OCEANSTREAM_S3_ENDPOINT",
                 "S3_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_S3_ENDPOINT", "s3.waw3-1.cloudferro.com")
    assert xfer.endpoint() == "https://s3.waw3-1.cloudferro.com"
    monkeypatch.setenv("XFER_S3_ENDPOINT", "https://other.example")
    assert xfer.endpoint() == "https://other.example"


def test_only_botocore_is_needed():
    text = (HPC / "xfer.py").read_text()
    assert "import boto3" not in text and "from boto3" not in text


def test_tar_push_uploads_every_member_and_reports_none_failed(monkeypatch):
    import io
    import tarfile

    files = {"a/.zarray": b"{}", "a/0.0": b"x" * 100, "a/0.1": b"y" * 50}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    stream = buf.getvalue()

    class Proc:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(stream)
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def wait(self):
            return 0

    remote = xfer.Remote("/dev/null", "u@h")
    monkeypatch.setattr(remote, "popen", lambda command, **kw: Proc())
    put = {}

    class Client:
        def put_object(self, Bucket, Key, Body):
            put[Key] = Body

    store = xfer.Store.__new__(xfer.Store)
    store.client = Client()
    items = [xfer.Item(f"/scratch/s.zarr/{n}", f"s3://b/z/s.zarr/{n}", len(d))
             for n, d in files.items()]
    assert xfer.push_tar(store, remote, items, "/scratch/s.zarr", streams=2) == 0
    assert put == {f"z/s.zarr/{n}": d for n, d in files.items()}


def test_a_remote_delete_never_leaves_the_destination():
    remote = xfer.Remote("/dev/null", "u@h")
    with pytest.raises(SystemExit):
        remote.delete("/scratch/x", ["../escape"])
    with pytest.raises(SystemExit):
        remote.delete("/scratch/x", ["/abs"])


def test_project_credential_names_are_read(monkeypatch):
    seen = {}

    class Session:
        def create_client(self, *a, **kw):
            seen.update(kw)
            return object()

    import botocore.session

    monkeypatch.setattr(botocore.session, "get_session", lambda: Session())
    for name in ("XFER_S3_ACCESS_KEY_ID", "OCEANSTREAM_S3_ACCESS_KEY_ID", "AWS_S3_ACCESS_KEY_ID",
                 "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARCO_S3_ACCESS_KEY_ID", "arco-key")
    xfer.Store()
    assert seen["aws_access_key_id"] == "arco-key"


def test_a_dry_run_moves_nothing(capsys):
    items = [xfer.Item("s3://b/a", "/x/a", 10)]
    assert xfer.dry_run(items, ["old"], "push") == 0
    out = capsys.readouterr().out
    assert "would move 1 files" in out and "would remove old" in out
