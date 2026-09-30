"""Send a staged tile tree to S3, one object per tile.

Twenty-two million objects is the other half of a rebuild's cost, and for a
long time it was the larger half: measured against CloudFerro from the build
pod, one Python process tops out at about 400 PUT/s no matter how many threads
it is given -- 64 threads reach 413/s at 1.13 cores, and 128 threads are
*slower* at 394/s while burning 1.37. The wall is the interpreter, not the
network (a keep-alive round trip is 10 ms) and not the endpoint. Eight
processes on the same pod reach about 1,930/s, which turns fifteen hours into
about three.

So the unit of parallelism here is a process, and the unit of work is a column.
The parent owns the resume marks and hands out coordinates; each worker opens
the staging database itself and PUTs the bytes it reads. Nothing but integers
crosses the process boundary, which matters at this size -- the tiles are 31 GB
and piping them to the parent would cost more than the PUTs.

This is shared by the flat tree and the label tree, which had grown two copies
of the same loop with different bugs. One of them had no resume marks at all.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

console = Console()

# Threads per process. Do not raise it: 64 was measured at 413 req/s and 128 at
# 394 req/s, so past this point more threads cost throughput as well as CPU.
DEFAULT_WORKERS = 64


def publish_workers() -> int:
    return max(1, int(os.environ.get("BASEMAP_XYZ_PUBLISH_WORKERS", DEFAULT_WORKERS)))


def publish_processes() -> int:
    """Processes to publish with. One by default, so nothing changes unasked."""
    return max(1, int(os.environ.get("BASEMAP_XYZ_PUBLISH_PROCESSES", "1")))


# ---------------------------------------------------------------------------
# Opening a staging database for reading
# ---------------------------------------------------------------------------

def open_stage(path: Path) -> sqlite3.Connection:
    """A read-only connection that does not need to write anything.

    ``immutable=1`` rather than ``mode=ro`` because a read-only WAL connection
    still creates the ``-shm`` mapping, and several processes doing that to one
    file on a network filesystem is exactly what WAL does not support.

    A leftover ``-wal`` has to be folded in first, though: to an immutable
    reader it is invisible, so a database that was not closed cleanly would
    silently publish as whatever it held at the last checkpoint. Recovering it
    needs one writable connection, which is why this is not simply a flag.
    """
    wal = path.with_name(path.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        console.print(f"  [yellow]{path.name}: folding in a leftover write-ahead log[/yellow]")
        recover = sqlite3.connect(path, isolation_level=None)
        recover.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        recover.execute("PRAGMA journal_mode=DELETE")
        recover.close()
    return sqlite3.connect(f"file:{path.as_uri()[7:]}?immutable=1", uri=True)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

# One S3 client and one open staging database per process, reused across the
# columns that process is handed. Same idiom as the renderer's _WORKER.
#
# Reset by _reset_state() at the top of every publish_stages call, because a
# process can publish more than once -- two versions in a row, or the flat tree
# and then the labels -- and a client or a database handle held over from the
# last call is the wrong one.
_STATE: dict = {}


def _reset_state() -> None:
    for key, value in list(_STATE.items()):
        if isinstance(key, tuple) and key[0] == "db":
            value.close()
    _STATE.clear()


# The object store rejected a key we were using, rather than refusing a request.
# Distinct from a transient error: retrying with the same credentials cannot help.
_STALE_CREDENTIALS = {
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
    "InvalidToken", "TokenRefreshRequired", "AuthFailure",
}


def _credentials() -> dict:
    """Credentials, read now rather than remembered from start-up.

    A publish runs for hours and the credentials it started with may be rotated
    out from under it. Where they come from matters: a Kubernetes Secret exposed
    as environment variables is frozen when the container starts, so a rotation
    leaves the process holding a key the store has already revoked and every
    request fails with InvalidAccessKeyId. The same Secret mounted as files is
    updated in place, so reading the file each time is what makes a rotation
    survivable.

    Falls back to the ambient environment, which is what a local run uses.
    """
    directory = os.environ.get("BASEMAP_S3_CREDENTIALS_DIR", "").strip()
    if directory:
        root = Path(directory)
        try:
            key = (root / "AWS_ACCESS_KEY_ID").read_text().strip()
            secret = (root / "AWS_SECRET_ACCESS_KEY").read_text().strip()
            if key and secret:
                return {"aws_access_key_id": key, "aws_secret_access_key": secret}
        except OSError:
            pass                      # fall through to the environment
    return {}


def _client(workers: int):
    client = _STATE.get("client")
    if client is None:
        import boto3
        from botocore.config import Config

        from basemap.config import S3_ENDPOINT

        attempts = max(1, int(os.environ.get("BASEMAP_S3_MAX_ATTEMPTS", "5")))
        client = _STATE["client"] = boto3.client(
            "s3", endpoint_url=S3_ENDPOINT,
            config=Config(max_pool_connections=workers * 2,
                          retries={"max_attempts": attempts}),
            **_credentials(),
        )
    return client


def _discard_client() -> None:
    _STATE.pop("client", None)


def _stage_db(stage: str) -> sqlite3.Connection:
    db = _STATE.get(("db", stage))
    if db is None:
        db = _STATE[("db", stage)] = open_stage(Path(stage))
    return db


def _publish_column(job: tuple) -> tuple[int, int, int]:
    """PUT every tile in one column. Returns (z, col, tiles sent)."""
    stage, z, col, prefix, extension, content_type, cache_control, workers = job

    from basemap.config import S3_BUCKET

    rows = _stage_db(stage).execute(
        "SELECT tile_row, tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ?",
        (z, col),
    ).fetchall()
    _client(workers)              # built once here, not raced for by 64 threads
    span = (1 << z) - 1

    def put(row) -> None:
        row_y, data = row
        y = span - row_y              # MBTiles rows count from the south
        # Looked up per object rather than bound once: the retry below replaces
        # the cached client, and a closure over the old one would send the
        # column again with the same revoked key.
        _client(workers).put_object(
            Bucket=S3_BUCKET, Key=f"{prefix}/{z}/{col}/{y}.{extension}", Body=data,
            ContentType=content_type, CacheControl=cache_control,
        )

    if not rows:
        return z, col, 0

    def send_all() -> None:
        with ThreadPoolExecutor(min(workers, len(rows))) as pool:
            list(pool.map(put, rows))

    try:
        send_all()
    except Exception as exc:                                      # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in _STALE_CREDENTIALS:
            raise
        # The key was rotated mid-run. Build a client from whatever the
        # credentials say now and send the column again -- a repeated PUT of the
        # same bytes to the same key is a no-op, so redoing one column is the
        # cheapest possible recovery. Without this the process dies and the Job
        # restarts, which costs a pod and the column in flight rather than
        # twenty seconds.
        _discard_client()
        _client(workers)          # rebuilt here so the threads do not race for it
        send_all()
    return z, col, len(rows)


# ---------------------------------------------------------------------------
# The parent
# ---------------------------------------------------------------------------

def _inventory(stages: Sequence[Path]) -> tuple[list[tuple], int]:
    """Every (stage, z, col, tiles) across the stages, in tile order.

    A column belongs to exactly one shard, so the same (z, col) appearing in two
    stages means the shards overlap -- a rendering mistake that would publish
    one column twice and leave the marks ambiguous. Caught here rather than
    halfway through a three-hour run.
    """
    seen: dict[tuple[int, int], str] = {}
    columns: list[tuple] = []
    total = 0
    for stage in stages:
        db = open_stage(stage)
        try:
            rows = db.execute(
                "SELECT zoom_level, tile_column, count(*) FROM tiles "
                "GROUP BY zoom_level, tile_column"
            ).fetchall()
        finally:
            db.close()
        for z, col, n in rows:
            if (z, col) in seen:
                raise SystemExit(
                    f"z{z}/{col} is in both {seen[(z, col)]} and {stage.name}; "
                    "the shards overlap and must not be published together."
                )
            seen[(z, col)] = stage.name
            columns.append((str(stage), z, col, n))
            total += n
    columns.sort(key=lambda r: (r[1], r[2]))
    return columns, total


def publish_stages(
    stages: Sequence[Path],
    *,
    prefix: str,
    marks_path: Path,
    extension: str = "webp",
    content_type: str = "image/webp",
    cache_control: str = "public, max-age=86400",
    processes: int | None = None,
    workers: int | None = None,
    redo_from: int | None = None,
    expect_tiles: int | None = None,
) -> int:
    """Publish every tile in *stages* under *prefix*. Returns the count sent.

    Resume is per column, recorded in *marks_path* and keyed by (z, col), which
    is globally unique across shards. At this size a run will be interrupted --
    an eviction, a credential expiry, a stall -- and starting over would mean
    re-PUTting tens of millions of objects that are already there.
    """
    from basemap.config import S3_BUCKET

    stages = [Path(s) for s in stages]
    missing = [s for s in stages if not s.is_file()]
    if missing:
        raise SystemExit("nothing staged at: " + ", ".join(str(s) for s in missing))

    processes = processes if processes is not None else publish_processes()
    workers = workers if workers is not None else publish_workers()

    _reset_state()
    columns, total_tiles = _inventory(stages)
    if expect_tiles is not None and total_tiles != expect_tiles:
        raise SystemExit(
            f"the stages hold {total_tiles:,} tiles but {expect_tiles:,} were expected "
            f"({expect_tiles - total_tiles:+,}).\n"
            "  Publishing an incomplete tree would leave holes in it; check the shards first."
        )

    marks_path.parent.mkdir(parents=True, exist_ok=True)
    marks = sqlite3.connect(marks_path, isolation_level=None)
    marks.execute("CREATE TABLE IF NOT EXISTS sent (z INT, col INT, PRIMARY KEY (z, col))")
    if redo_from is not None:
        cleared = marks.execute("DELETE FROM sent WHERE z >= ?", (redo_from,)).rowcount
        console.print(f"  [yellow]Redoing from z{redo_from}: {cleared:,} columns "
                      "to send again.[/yellow]")
    already = {tuple(r) for r in marks.execute("SELECT z, col FROM sent")}

    todo = [c for c in columns if (c[1], c[2]) not in already]
    pending_tiles = sum(c[3] for c in todo)
    console.print(
        f"  {pending_tiles:,} tiles to send → s3://{S3_BUCKET}/{prefix}/  "
        f"({processes} processes × {workers} threads)"
        + (f", {total_tiles - pending_tiles:,} already sent" if already else "")
    )
    if not todo:
        console.print("[green]Nothing to do.[/green]")
        marks.close()
        return 0

    jobs = [
        (stage, z, col, prefix, extension, content_type, cache_control, workers)
        for stage, z, col, _ in todo
    ]

    sent, started = 0, time.time()
    with Progress(console=console) as progress:
        task = progress.add_task("Tiles", total=pending_tiles)

        def record(z: int, col: int, n: int) -> None:
            nonlocal sent
            marks.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (z, col))
            sent += n
            progress.advance(task, n)
            if sent % 200_000 < n:
                rate = sent / max(1e-9, time.time() - started)
                console.print(f"  [dim]{sent:,} sent, {rate:,.0f}/s[/dim]")

        if processes == 1:
            # The same code path, so the single-process case cannot drift away
            # from the parallel one.
            for job in jobs:
                record(*_publish_column(job))
        else:
            # Spawned, not forked. The renderer forks on purpose, because its
            # workers share twelve gigabytes of mmapped archives and copy-on-write
            # is the whole point. Nothing here is worth sharing -- a worker needs
            # an S3 client and a database handle it opens itself -- and forking
            # while the progress bar's thread is running is the documented way to
            # deadlock. Startup costs a second per process, once, against hours.
            with ProcessPoolExecutor(
                max_workers=processes,
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                queue = iter(jobs)
                inflight = {}
                while True:
                    for job in queue:
                        inflight[pool.submit(_publish_column, job)] = job
                        if len(inflight) >= processes * 2:
                            break
                    if not inflight:
                        break
                    ready, _ = wait(inflight, return_when="FIRST_COMPLETED")
                    for future in ready:
                        inflight.pop(future)
                        record(*future.result())

    marks.close()
    rate = sent / max(1e-9, time.time() - started)
    console.print(f"[green]Published:[/green] {sent:,} tiles at {rate:,.0f}/s "
                  f"→ s3://{S3_BUCKET}/{prefix}/")
    return sent
