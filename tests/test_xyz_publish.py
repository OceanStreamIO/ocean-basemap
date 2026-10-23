"""The publisher: what it sends, what it refuses, and what it does twice."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from basemap.pipeline.publish import publish_processes, publish_stages, publish_workers
from basemap.pipeline.xyz import MAX_ZOOM, Shard

MAX_Z = 3                                     # 85 tiles; the arithmetic is the same
TREE = sum(4**z for z in range(MAX_Z + 1))


class FakeS3:
    """Records what would have been PUT. Only usable in-process."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put_object(self, **kw):
        self.objects[kw["Key"]] = (kw["Body"], kw["ContentType"], kw["CacheControl"])


def write_stage(path, shard, max_zoom=MAX_Z):
    """A staging database holding *shard*'s tiles, each naming its own address."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT,"
               " tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row))")
    for z in range(max_zoom + 1):
        for x in shard.columns(z):
            for y in range(1 << z):
                db.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                           (z, x, (1 << z) - 1 - y, f"{z}/{x}/{y}".encode()))
    db.commit()
    db.close()
    return path


@pytest.fixture
def shards(tmp_path):
    return [write_stage(tmp_path / f"s.{i}.mbtiles", Shard(i, 4)) for i in range(4)]


def publish_module_inventory(stages):
    from basemap.pipeline.publish import _inventory

    return _inventory(list(stages))


def send(stages, marks, **kw):
    fake = FakeS3()
    with patch("boto3.client", return_value=fake):
        sent = publish_stages(stages, prefix="basemap/t/xyz", marks_path=marks, workers=4, **kw)
    return sent, fake.objects


def test_the_rows_are_flipped_back_north_up(tmp_path, shards):
    """MBTiles counts rows from the south and the tree counts them from the north.

    Each tile's body names the address it was written for, so a key that does
    not match its body means the flip is wrong -- which would publish a tree
    that is upside down per column and look plausible in a listing.
    """
    _, objects = send(shards, tmp_path / "m.sqlite")
    for key, (body, _, _) in objects.items():
        assert body.decode() == key[len("basemap/t/xyz/"):-len(".webp")]


def test_every_tile_goes_out_once_with_the_right_headers(tmp_path, shards):
    sent, objects = send(shards, tmp_path / "m.sqlite", expect_tiles=TREE)
    assert sent == TREE == len(objects)
    assert all(ct == "image/webp" for _, ct, _ in objects.values())
    assert all(cc == "public, max-age=86400" for _, _, cc in objects.values())


def test_the_worker_payload_survives_being_sent_to_a_spawned_process(tmp_path, shards):
    """Workers are spawned, so everything handed to one has to pickle.

    Forking would inherit the parent's objects and hide a mistake here; spawn
    re-imports and unpickles, so a closure or a Path in the job tuple would fail
    only once a real run started. Cheap to check, expensive to discover.
    """
    import pickle

    from basemap.pipeline.publish import _publish_column

    columns, _ = publish_module_inventory(shards)
    job = (columns[0][0], columns[0][1], columns[0][2], "basemap/t/xyz",
           "webp", "image/webp", "public, max-age=86400", 4)
    assert pickle.loads(pickle.dumps(job)) == job
    assert pickle.loads(pickle.dumps(_publish_column)) is _publish_column


def test_a_send_that_fails_is_not_marked_as_sent(tmp_path, shards, monkeypatch):
    """A marked column is never looked at again, so marking a failure is a hole.

    Runs the parallel path for real, against a closed port. Spawned children
    re-import the config, so the bogus endpoint reaches them -- which is also
    what keeps this test from ever touching the real bucket.
    """
    monkeypatch.setenv("BASEMAP_S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BASEMAP_S3_MAX_ATTEMPTS", "1")      # do not sit through backoff

    marks = tmp_path / "m.sqlite"
    with pytest.raises(Exception):
        publish_stages(shards, prefix="basemap/t/xyz", marks_path=marks,
                       processes=2, workers=2)

    sent = {tuple(r) for r in sqlite3.connect(marks).execute("SELECT z, col FROM sent")}
    assert sent == set(), "a column that failed to send must not be marked"


def test_a_second_run_sends_nothing(tmp_path, shards):
    """At twenty-two million objects an interrupted run must not start over."""
    first, _ = send(shards, tmp_path / "m.sqlite")
    again, objects = send(shards, tmp_path / "m.sqlite")
    assert first == TREE
    assert (again, objects) == (0, {})


def test_marks_are_per_prefix_so_a_new_version_resends_everything(tmp_path, shards):
    send(shards, tmp_path / "live.sqlite")
    resent, _ = send(shards, tmp_path / "v2.sqlite")
    assert resent == TREE


def test_marks_written_for_one_prefix_are_refused_for_another(tmp_path, shards):
    send(shards, tmp_path / "m.sqlite")
    with pytest.raises(SystemExit, match="basemap/t/xyz"):
        with patch("boto3.client", return_value=FakeS3()):
            publish_stages(shards, prefix="basemap/v11/xyz", marks_path=tmp_path / "m.sqlite",
                           workers=4)


def test_marks_from_an_unrecorded_destination_are_refused(tmp_path, shards):
    old = sqlite3.connect(tmp_path / "old.sqlite")
    old.execute("CREATE TABLE sent (z INT, col INT, PRIMARY KEY (z, col))")
    old.execute("INSERT INTO sent VALUES (0, 0)")
    old.commit()
    old.close()
    with pytest.raises(SystemExit, match="unrecorded"):
        send(shards, tmp_path / "old.sqlite")


def test_redoing_from_a_zoom_sends_that_zoom_again(tmp_path, shards):
    send(shards, tmp_path / "m.sqlite")
    resent, objects = send(shards, tmp_path / "m.sqlite", redo_from=3)
    assert resent == 4**3 == len(objects)
    assert all(key.startswith("basemap/t/xyz/3/") for key in objects)


def test_overlapping_shards_are_refused_before_anything_is_sent(tmp_path):
    """One column in two stages makes the resume marks ambiguous."""
    a = write_stage(tmp_path / "a.mbtiles", Shard(0, 2))
    b = write_stage(tmp_path / "b.mbtiles", Shard(0, 2))
    with pytest.raises(SystemExit, match="overlap"):
        send([a, b], tmp_path / "m.sqlite")


def test_a_short_tree_is_refused_when_a_count_is_given(tmp_path, shards):
    """Publishing three shards of four would leave a quarter of the map missing."""
    with pytest.raises(SystemExit, match="incomplete tree"):
        send(shards[:3], tmp_path / "m.sqlite", expect_tiles=TREE)


def test_a_missing_stage_is_named(tmp_path):
    with pytest.raises(SystemExit, match="nothing staged"):
        send([tmp_path / "absent.mbtiles"], tmp_path / "m.sqlite")


def test_the_defaults_are_one_process_and_sixty_four_threads(monkeypatch):
    """Unset, the publisher behaves exactly as it did before it could fan out.

    Sixty-four is measured, not chosen: 64 threads reached 413 req/s and 128
    reached 394, so raising it costs throughput as well as CPU.
    """
    monkeypatch.delenv("BASEMAP_XYZ_PUBLISH_PROCESSES", raising=False)
    monkeypatch.delenv("BASEMAP_XYZ_PUBLISH_WORKERS", raising=False)
    assert (publish_processes(), publish_workers()) == (1, 64)
    monkeypatch.setenv("BASEMAP_XYZ_PUBLISH_PROCESSES", "8")
    assert publish_processes() == 8


def test_a_leftover_write_ahead_log_is_folded_in_not_ignored(tmp_path):
    """An immutable reader cannot see a -wal, so it must be recovered first.

    Without this the publisher would silently send whatever the database held
    at its last checkpoint -- a partial tree that looks complete.
    """
    path = tmp_path / "s.mbtiles"
    # Written by a process that dies without closing, which is the only way a
    # -wal survives: a clean close checkpoints and removes it.
    subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import os, sqlite3
        db = sqlite3.connect({str(path)!r}, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT,"
                   " tile_row INT, tile_data BLOB,"
                   " PRIMARY KEY (zoom_level, tile_column, tile_row))")
        db.execute("INSERT INTO tiles VALUES (0, 0, 0, 'x')")
        os._exit(0)
    """)], check=True)
    assert (tmp_path / "s.mbtiles-wal").stat().st_size > 0

    sent, objects = send([path], tmp_path / "m.sqlite")
    assert sent == 1 and list(objects) == ["basemap/t/xyz/0/0/0.webp"]


def test_the_real_tree_is_the_number_the_publisher_will_be_asked_for():
    assert Shard().expected_tiles(MAX_ZOOM) == 22_369_621


# ---------------------------------------------------------------------------
# Credentials, which a run this long outlives
# ---------------------------------------------------------------------------

def credentials_dir(tmp_path, key, secret):
    root = tmp_path / "s3"
    root.mkdir(exist_ok=True)
    (root / "AWS_ACCESS_KEY_ID").write_text(key)
    (root / "AWS_SECRET_ACCESS_KEY").write_text(secret)
    return root


def test_credentials_are_read_at_use_so_a_rotation_is_picked_up(tmp_path, monkeypatch):
    """The platform rotates the Secret under a publish that runs for hours.

    Read once at start-up -- which is what an environment variable is -- the
    process holds a revoked key for the rest of the run. Read from the mounted
    files each time, the kubelet's update reaches it.
    """
    from basemap.pipeline.publish import _credentials

    root = credentials_dir(tmp_path, "first\n", "first-secret\n")
    monkeypatch.setenv("BASEMAP_S3_CREDENTIALS_DIR", str(root))
    assert _credentials() == {"aws_access_key_id": "first",
                              "aws_secret_access_key": "first-secret"}

    (root / "AWS_ACCESS_KEY_ID").write_text("second\n")
    assert _credentials()["aws_access_key_id"] == "second"


def test_credentials_fall_back_to_the_environment(tmp_path, monkeypatch):
    """A local run has no mounted Secret, and boto3 resolves it the usual way."""
    from basemap.pipeline.publish import _credentials

    monkeypatch.delenv("BASEMAP_S3_CREDENTIALS_DIR", raising=False)
    assert _credentials() == {}

    monkeypatch.setenv("BASEMAP_S3_CREDENTIALS_DIR", str(tmp_path / "absent"))
    assert _credentials() == {}, "a directory that is not there is not an error"


class RotatingS3(FakeS3):
    """Rejects everything until the client is rebuilt, as a revoked key does."""

    def __init__(self, live: dict, on_reject=None):
        super().__init__()
        self.live = live                       # the key this store still accepts
        self.on_reject = on_reject
        self.mine = None
        self.rejected = 0

    def put_object(self, **kw):
        if self.mine != self.live["key"]:
            self.rejected += 1
            if self.on_reject is not None:
                self.on_reject()               # the rotation that revoked us
            raise ClientError({"Error": {"Code": "InvalidAccessKeyId"}}, "PutObject")
        super().put_object(**kw)


def test_a_rotated_key_costs_a_column_not_the_process(tmp_path, monkeypatch):
    """Recovery has to reach the threads, which is the part that was wrong once.

    The threads send through whatever the cache holds, so a client bound before
    the pool starts would be retried with the same revoked key and the column
    would fail anyway. Two stores stand in for before and after the rotation.
    """
    from basemap.pipeline import publish as mod

    stage = write_stage(tmp_path / "s.mbtiles", Shard(0, 1), max_zoom=1)
    live = {"key": "new"}
    root = credentials_dir(tmp_path, "old", "old-secret")
    monkeypatch.setenv("BASEMAP_S3_CREDENTIALS_DIR", str(root))

    built = []

    def rotate() -> None:
        # What the kubelet does: the mounted file now holds the live key. The
        # process cannot see this until it reads the file again.
        (root / "AWS_ACCESS_KEY_ID").write_text(live["key"])

    def fake_client(*_a, **kw):
        store = RotatingS3(live, on_reject=rotate)
        store.mine = kw.get("aws_access_key_id")
        built.append(store)
        return store

    mod._reset_state()
    with patch("boto3.client", side_effect=fake_client):
        job = (str(stage), 1, 0, "basemap/t/xyz", "webp", "image/webp", "c", 2)
        assert mod._publish_column(job) == (1, 0, 2)
    mod._reset_state()

    assert len(built) == 2, "the stale client has to be discarded, not reused"
    assert built[0].rejected and built[0].objects == {}
    assert sorted(built[1].objects) == ["basemap/t/xyz/1/0/0.webp", "basemap/t/xyz/1/0/1.webp"]


def test_a_transient_failure_costs_a_retry_not_the_shard(tmp_path, monkeypatch):
    """An exception here ends the process, the pod, and the shard it was on.

    Measured on the z14 run: a restart re-sends about two and three quarter
    hours of a shard, because a pod's marks do not outlive it. The errors that
    triggered it came back with an empty code -- a 500 or a dropped connection,
    once botocore has exhausted its own retries -- so this must not be limited
    to the codes that name a credential problem.
    """
    from basemap.pipeline import publish as mod

    stage = write_stage(tmp_path / "s.mbtiles", Shard(0, 1), max_zoom=0)
    monkeypatch.delenv("BASEMAP_S3_CREDENTIALS_DIR", raising=False)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    attempts = []

    class FlakyS3(FakeS3):
        def put_object(self, **kw):
            attempts.append(1)
            if len(attempts) < 3:
                raise ClientError({"Error": {"Code": ""}}, "PutObject")
            super().put_object(**kw)

    store = FlakyS3()
    mod._reset_state()
    with patch("boto3.client", return_value=store):
        assert mod._publish_column((str(stage), 0, 0, "basemap/t/xyz", "webp",
                                    "image/webp", "c", 1)) == (0, 0, 1)
    mod._reset_state()
    assert len(attempts) == 3, "it should have tried again rather than raising"
    assert sorted(store.objects) == ["basemap/t/xyz/0/0/0.webp"]


def test_a_failure_that_never_clears_still_raises(tmp_path, monkeypatch):
    """Retrying forever would hide a real fault behind a stalled publish."""
    from basemap.pipeline import publish as mod

    stage = write_stage(tmp_path / "s.mbtiles", Shard(0, 1), max_zoom=0)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    class Broken(FakeS3):
        def put_object(self, **kw):
            raise ClientError({"Error": {"Code": ""}}, "PutObject")

    mod._reset_state()
    with patch("boto3.client", return_value=Broken()), pytest.raises(ClientError):
        mod._publish_column((str(stage), 0, 0, "basemap/t/xyz", "webp",
                             "image/webp", "c", 1))
    mod._reset_state()


def test_an_error_that_is_not_about_credentials_still_fails(tmp_path, monkeypatch):
    """Retrying every error would turn a real fault into a silent double send."""
    from basemap.pipeline import publish as mod

    stage = write_stage(tmp_path / "s.mbtiles", Shard(0, 1), max_zoom=0)
    monkeypatch.delenv("BASEMAP_S3_CREDENTIALS_DIR", raising=False)

    class Broken(FakeS3):
        def put_object(self, **kw):
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")

    mod._reset_state()
    with patch("boto3.client", return_value=Broken()), pytest.raises(ClientError):
        mod._publish_column((str(stage), 0, 0, "basemap/t/xyz", "webp",
                             "image/webp", "c", 1))
    mod._reset_state()
