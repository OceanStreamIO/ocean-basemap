#!/usr/bin/env python3
"""Point /xyz/* at a freshly built tile tree, atomically.

Publishing 22 million objects takes about as long as rendering them, so
writing into the live prefix leaves the public map a mix of old and new tiles
for most of a day, with a seam moving across it as the upload walks the
columns. A rebuild therefore publishes under its own prefix::

    s3://oceanstream-bucket/basemap/rev-<rev>/xyz/{z}/{x}/{y}.webp

and this points the CDN at it. A cache behaviour for ``/xyz/*`` targets an
origin whose OriginPath carries the revision, so the public URL never changes
-- https://basemap.oceanstream.io/xyz/{z}/{x}/{y}.webp -- and the switch is one
CloudFront deployment rather than 22 million copies.

CloudFront resolves a request as OriginPath + URI, and it does not strip the
path pattern. With OriginPath ``/basemap/rev-<rev>`` a request for
``/xyz/8/136/91.webp`` is fetched from ``basemap/rev-<rev>/xyz/8/136/91.webp``,
which is exactly where the publish step wrote it.

    ./deploy/cdn/switch-xyz.py --show
    ./deploy/cdn/switch-xyz.py --dry-run f304062
    ./deploy/cdn/switch-xyz.py f304062

The previous tree stays in the bucket, so rolling back is this script with the
old revision. Deleting it is deliberate and separate: it is 22 million objects
and takes hours.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = (HERE / "distribution-id").read_text().strip()
PUBLIC = (HERE / "origin").read_text().strip()
ORIGIN_ID = "cloudferro-xyz"
PATTERN = "/xyz/*"


def aws(*args: str) -> str:
    """CloudFront is AWS proper; the keys in the environment are CloudFerro's."""
    env = dict(os.environ)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        env.pop(key, None)
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    out = subprocess.run(["aws", "cloudfront", *args], check=True, env=env,
                         capture_output=True, text=True)
    return out.stdout


def fetch() -> tuple[dict, str]:
    payload = json.loads(aws("get-distribution-config", "--id", DIST))
    return payload["DistributionConfig"], payload["ETag"]


def live_path(config: dict) -> str | None:
    for origin in config["Origins"]["Items"]:
        if origin["Id"] == ORIGIN_ID:
            return origin.get("OriginPath") or ""
    return None


def retarget(config: dict, rev: str) -> dict:
    """Add or repoint the /xyz/* origin and behaviour, leaving the rest alone."""
    config = json.loads(json.dumps(config))
    default = config["Origins"]["Items"][0]
    path = f"/basemap/rev-{rev}"

    origins = [o for o in config["Origins"]["Items"] if o["Id"] != ORIGIN_ID]
    mirror = json.loads(json.dumps(default))
    mirror["Id"] = ORIGIN_ID
    mirror["OriginPath"] = path
    origins.append(mirror)
    config["Origins"] = {"Quantity": len(origins), "Items": origins}

    behaviour = json.loads(json.dumps(config["DefaultCacheBehavior"]))
    behaviour["PathPattern"] = PATTERN
    behaviour["TargetOriginId"] = ORIGIN_ID
    existing = [b for b in (config.get("CacheBehaviors") or {}).get("Items", [])
                if b.get("PathPattern") != PATTERN]
    items = [behaviour, *existing]
    config["CacheBehaviors"] = {"Quantity": len(items), "Items": items}
    return config


def check(rev: str) -> None:
    """A tile must come back, or the switch has pointed at an empty prefix."""
    url = f"{PUBLIC}/xyz/4/8/5.webp"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            print(f"  {url} -> {r.status}, {len(r.read()):,} bytes")
    except Exception as exc:                                   # noqa: BLE001
        print(f"  {url} -> {exc}")
        print("  CloudFront takes a few minutes to deploy; re-check before worrying.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("rev", nargs="?", help="the build revision to serve")
    ap.add_argument("--show", action="store_true", help="what is live now")
    ap.add_argument("--dry-run", action="store_true", help="print, change nothing")
    args = ap.parse_args()

    config, etag = fetch()
    path = live_path(config)

    if args.show or not args.rev:
        if path is None:
            print(f"{PATTERN} has no behaviour of its own; it falls through to "
                  f"{config['Origins']['Items'][0].get('OriginPath') or '/'}")
        else:
            print(f"{PATTERN} is served from {path}")
        return 0 if args.show else 2

    want = f"/basemap/rev-{args.rev}"
    if path == want:
        print(f"{PATTERN} already serves {want}; nothing to do")
        return 0

    print(f"{PATTERN}: {path or '(default origin)'} -> {want}")
    if args.dry_run:
        print("dry run; nothing changed")
        return 0

    updated = retarget(config, args.rev)
    body = Path("/tmp/xyz-distribution.json")
    body.write_text(json.dumps(updated))
    aws("update-distribution", "--id", DIST, "--if-match", etag,
        "--distribution-config", f"file://{body}")
    print("  updated; CloudFront is deploying")
    check(args.rev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
