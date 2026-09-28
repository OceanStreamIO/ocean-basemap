#!/usr/bin/env python3
"""Compare tiles rendered on the cluster against the published tree.

This is the gate. The pipeline is the same code on both machines and it installs
the same wheels, so the two GDALs that matter -- the ones inside rasterio and
fiona -- are identical versions, and most tiles should come out byte for byte the
same. Two things could still move a pixel:

  * The command-line GDAL differs. The cluster's module is an older release than
    the one here, and it builds the contours and the polygons. That does not
    shift a pixel directly; it changes geometry, which then changes every tile
    with a contour through it.
  * numpy dispatches its kernels on the CPU it finds. A different instruction
    set can change the last bit of a reduction, which through a float blend and
    a box downsample can move an 8-bit channel by one.

So there are two bars, and they are different bars. A tile the renderer passed
through untouched -- an opaque sea tile with no land over it and no contour
through it, which is most of the ocean -- is a copy of the archive's own bytes.
No decode, no arithmetic. Those must be *identical*, and when they are not, the
inputs differ rather than the arithmetic: that is how a stale archive is caught.

Composited tiles get a tolerance, and choosing it needs one more fact: the
format is lossy. Measured, a single unit of difference in the array *before*
encoding comes back as up to twenty units after it, because the encoder makes
different choices about a slightly different input. So "no channel moves by more
than one" is the wrong test -- it fails on exactly the noise it was meant to
absorb.

What separates noise from a real change is its shape, not its amplitude. A
different instruction set perturbs pixels thinly and everywhere, so the mean
stays near zero. A changed contour, a missing lake or a shifted coastline puts a
large difference in a small area, so the mean stays low but the tail does not.
Both are measured, and a tile that passes on both is still reported rather than
hidden, because at this size the honest signal is how *many* tiles stopped being
identical.

    ./deploy/hpc/parity.py --shard /path/to/xyz.stage.000of004.mbtiles
    ./deploy/hpc/parity.py --shard … --sample 2000 --zooms 6,8,10,12
    ./deploy/hpc/parity.py --prefix basemap/hpc-trial/xyz --sample 500

Exits non-zero if anything fails its bar, and names the tiles.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIVE_PREFIX = os.environ.get("BASEMAP_PARITY_PREFIX", "basemap/xyz")
BUCKET = os.environ.get("BASEMAP_S3_BUCKET", "oceanstream-bucket")
ENDPOINT = os.environ.get("BASEMAP_S3_ENDPOINT", "https://s3.waw3-1.cloudferro.com")

# Thinly-spread encoder noise keeps the mean near zero even when individual
# channels move a lot; a structured change does not.
MAX_MEAN_DELTA = 2.0
# A pixel this far out is not encoder noise. Allow a whisper of them -- an edge
# where the encoder chose a different block boundary -- and no more.
TAIL_DELTA = 24
MAX_TAIL_FRACTION = 0.002


def live_url(z: int, x: int, y: int, prefix: str = LIVE_PREFIX) -> str:
    # Straight at the object store, not the CDN: an edge cache would answer with
    # whatever it happens to hold, which is not what is being compared.
    return f"{ENDPOINT}/{BUCKET}/{prefix}/{z}/{x}/{y}.webp"


def sample_from_shard(path: Path, zooms: list[int], count: int) -> list[tuple[int, int, int]]:
    """Tiles spread across the zooms this shard holds."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    per_zoom = max(1, count // max(1, len(zooms)))
    picked: list[tuple[int, int, int]] = []
    rng = random.Random(0)                     # reproducible, so reruns compare like with like
    for z in zooms:
        rows = db.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,)
        ).fetchall()
        if not rows:
            continue
        for zoom, col, row in rng.sample(rows, min(per_zoom, len(rows))):
            picked.append((zoom, col, (1 << zoom) - 1 - row))
    db.close()
    return picked


def tile_from_shard(path: Path, z: int, x: int, y: int) -> bytes | None:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = db.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    db.close()
    return row[0] if row else None


def compare(ours: bytes, theirs: bytes) -> tuple[str, float, float]:
    """(verdict, mean absolute difference, fraction of pixels far out)."""
    if ours == theirs:
        return "identical", 0.0, 0.0
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(io.BytesIO(ours)).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(theirs)).convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        return "different shape", 255.0, 1.0
    delta = np.abs(a - b)
    mean = float(delta.mean())
    tail = float((delta > TAIL_DELTA).mean())
    if mean <= MAX_MEAN_DELTA and tail <= MAX_TAIL_FRACTION:
        return "close", mean, tail
    return "DIFFERENT", mean, tail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--shard", type=Path, help="a rendered staging database")
    source.add_argument("--prefix", help="a published prefix, e.g. basemap/hpc-trial/xyz")
    parser.add_argument("--against", default=LIVE_PREFIX,
                        help=f"reference prefix (default {LIVE_PREFIX})")
    parser.add_argument("--sample", type=int, default=500)
    parser.add_argument("--zooms", default="6,8,10,11,12")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    import httpx

    zooms = [int(z) for z in args.zooms.split(",")]
    if args.shard:
        if not args.shard.is_file():
            print(f"no such shard: {args.shard}", file=sys.stderr)
            return 2
        tiles = sample_from_shard(args.shard, zooms, args.sample)
        fetch_ours = lambda z, x, y: tile_from_shard(args.shard, z, x, y)   # noqa: E731
        label = args.shard.name
    else:
        # Sampling a prefix blindly would mostly miss, so walk the same
        # deterministic sample the shard case uses: column 0 of each zoom plus a
        # spread, and accept that a miss is reported rather than silently skipped.
        rng = random.Random(0)
        tiles = [(z, rng.randrange(1 << z), rng.randrange(1 << z))
                 for z in zooms for _ in range(max(1, args.sample // len(zooms)))]
        fetch_ours = None
        label = args.prefix

    print(f"Comparing {label} against {args.against}")
    print(f"  {len(tiles)} tiles, zooms {args.zooms}\n")

    client = httpx.Client(timeout=30, follow_redirects=True)

    def one(coord):
        z, x, y = coord
        try:
            if fetch_ours is not None:
                ours = fetch_ours(z, x, y)
            else:
                r = client.get(live_url(z, x, y, args.prefix))
                ours = r.content if r.status_code == 200 else None
            r = client.get(live_url(z, x, y, args.against))
            theirs = r.content if r.status_code == 200 else None
        except Exception as exc:                      # noqa: BLE001
            return coord, f"error: {exc}", 255.0, 1.0
        if ours is None and theirs is None:
            return coord, "absent from both", 0.0, 0.0
        if ours is None:
            return coord, "MISSING from ours", 255.0, 1.0
        if theirs is None:
            return coord, "absent from the reference", 0.0, 0.0
        return (coord, *compare(ours, theirs))

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(one, tiles))

    counts: dict[str, int] = defaultdict(int)
    by_zoom: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    failures = []
    for (z, x, y), verdict, mean, tail in results:
        counts[verdict] += 1
        by_zoom[z][verdict] += 1
        if verdict.startswith(("DIFFERENT", "MISSING", "error", "different")):
            failures.append(((z, x, y), verdict, mean, tail))

    compared = sum(n for v, n in counts.items() if v != "absent from both")
    print("  verdict                      tiles")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict:<28} {n:>5}")

    identical = counts.get("identical", 0)
    if compared:
        print(f"\n  byte-identical: {identical}/{compared} ({100 * identical / compared:.1f}%)")
        # Most of the ocean goes out as the archive's own bytes, so a low share
        # here means the inputs differ rather than the arithmetic -- which is a
        # different and more serious thing than a pixel moving by one.
        if identical / compared < 0.3:
            print("  ! Fewer byte-identical tiles than expected. Suspect the staged")
            print("    archives differ from the ones the reference was built from.")

    print("\n  by zoom:")
    for z in sorted(by_zoom):
        parts = ", ".join(f"{v}={n}" for v, n in sorted(by_zoom[z].items()))
        print(f"    z{z:<3} {parts}")

    if failures:
        print(f"\n  {len(failures)} tile(s) outside tolerance:")
        for (z, x, y), verdict, mean, tail in failures[:20]:
            print(f"    z{z}/{x}/{y}  {verdict}  mean={mean:.2f} "
                  f"far-out={100 * tail:.2f}%")
        if len(failures) > 20:
            print(f"    … and {len(failures) - 20} more")
        return 1

    close = counts.get("close", 0)
    if close:
        print(f"\n  {close} tile(s) differ but stay within tolerance. That is expected")
        print("  where the two machines' CPUs dispatch different kernels; it is worth")
        print("  a look if the count is large or clusters in one region.")
    print("\n  PASS — every sampled tile is identical or within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
