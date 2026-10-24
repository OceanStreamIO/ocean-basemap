#!/usr/bin/env python3
"""Delete a published flat tile tree, one version at a time.

A z13 tree is 89 million objects and a z14 one is 358 million, so this is not a
job for ``aws s3 rm``: deletes batch a thousand keys per call, and the way to
use that is the same decomposition the publisher uses -- one process per column
of the tree, listing and deleting as it goes.

It refuses to delete the version the published viewer is pointing at. That is
not a formality: the preview and the tree it reads are changed by two different
commands, and the window between them is exactly when someone deletes the tree
still on screen.

    ./delete-version.py v2 --dry-run
    ./delete-version.py v2
    ./delete-version.py legacy      # basemap/xyz/, the tree from before versions
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from urllib.request import urlopen

VIEWER = "https://basemap.oceanstream.io/viewer/static/xyz.html"
ENDPOINT = os.environ.get("BASEMAP_S3_ENDPOINT", "https://s3.waw3-1.cloudferro.com")
BUCKET = os.environ.get("BASEMAP_S3_BUCKET", "oceanstream-bucket")
_CLIENT = {}


def client():
    if "c" not in _CLIENT:
        import boto3
        from botocore.config import Config
        _CLIENT["c"] = boto3.client("s3", endpoint_url=ENDPOINT,
                                    config=Config(retries={"max_attempts": 10}))
    return _CLIENT["c"]


def viewer_default() -> str | None:
    """The version the published preview page defaults to, or None."""
    try:
        with urlopen(VIEWER, timeout=30) as response:      # noqa: S310
            body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip
            body = gzip.decompress(body)
        found = re.search(rb"get\('v'\)\s*\|\|\s*'([a-z0-9]+)'", body)
        return found.group(1).decode() if found else None
    except OSError:
        return None


def delete_prefix(prefix: str) -> int:
    """Delete every object under one prefix. Returns how many went."""
    s3, gone = client(), 0
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    for page in pages:
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch, "Quiet": True})
            gone += len(batch)
    return gone


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="the tree to delete, e.g. v2, or 'legacy' for basemap/xyz/")
    ap.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    ap.add_argument("--processes", type=int, default=8)
    ap.add_argument("--force", action="store_true",
                    help="delete even if the viewer points at it")
    args = ap.parse_args()

    if not re.fullmatch(r"v[0-9]+|legacy", args.version):
        print(f"refusing: {args.version!r} is not a version like v2, or legacy", file=sys.stderr)
        return 2

    showing = viewer_default()
    if showing == args.version and not args.force:
        print(f"refusing: the published viewer still defaults to {showing}.\n"
              f"  Switch the preview first, or pass --force if that is deliberate.",
              file=sys.stderr)
        return 2
    if showing is None:
        print("note: could not read the viewer's current version", file=sys.stderr)

    # The tree published straight into basemap/xyz/ before versions existed.
    # The CDN's /xyz/ has pointed at a version prefix since, so nothing serves it.
    root = "basemap/xyz/" if args.version == "legacy" else f"basemap/{args.version}/xyz/"

    # Which levels this tree actually has. Worth the sixteen probes: a version
    # rendered to z12 has none of z13 upward, and sweeping them anyway would be
    # fifty-seven thousand round trips that can only come back empty.
    s3 = client()
    zooms = [z for z in range(16)
             if s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{root}{z}/",
                                   MaxKeys=1).get("KeyCount", 0)]
    if not zooms:
        print(f"{root}: nothing there")
        return 0

    # One prefix per (z, x) above z7, so the work splits without listing anything
    # twice -- every key lives under exactly one. The column count is 2^z and
    # not a constant: fixing it at 128 would sweep an eighth of z10 and leave
    # the rest of the tree behind, still billed and still served.
    prefixes = [f"{root}{z}/" for z in zooms if z < 8]
    prefixes += [f"{root}{z}/{x}/" for z in zooms if z >= 8 for x in range(1 << z)]

    if args.dry_run:
        print(f"{root}: levels z{zooms[0]}-z{zooms[-1]}, "
              f"{len(prefixes):,} prefixes to sweep, viewer shows {showing}")
        return 0

    print(f"deleting {root} with {args.processes} processes")
    total = 0
    with ProcessPoolExecutor(args.processes) as pool:
        for n in pool.map(delete_prefix, prefixes):
            total += n
            if total and n:
                print(f"  {total:,} objects deleted", flush=True)
    print(f"done: {total:,} objects removed from {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
