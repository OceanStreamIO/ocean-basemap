#!/usr/bin/env python3
"""Answer a missing tile with 404, not 403.

The bucket does not let the public list it, so for an object that is not there
it answers 403 Forbidden with an XML body. The tree is sparse past z12 -- the
survey detail exists only where the surveys are -- so every client asking for a
z13 tile off a survey got "forbidden", which reads as a permissions fault and
which some clients treat differently from "no tile here". This tells CloudFront
to turn the bucket's 403 into a 404 with a one-line text body, cached for a
minute so a tile published later is not hidden for long.

    ./deploy/cdn/not-found.py --show
    ./deploy/cdn/not-found.py --dry-run
    ./deploy/cdn/not-found.py

The body is basemap/404.txt in the bucket, reached through the default
behaviour; this script puts it there.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = (HERE / "distribution-id").read_text().strip()
PAGE = "/404.txt"
BODY = "Not found: there is no tile here.\n"
CACHE_S = 60
WANTED = {
    "Quantity": 1,
    "Items": [{"ErrorCode": 403, "ResponsePagePath": PAGE, "ResponseCode": "404",
               "ErrorCachingMinTTL": CACHE_S}],
}


def aws(*args: str) -> str:
    """CloudFront is AWS proper; the keys in the environment are CloudFerro's."""
    env = dict(os.environ)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        env.pop(key, None)
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    return subprocess.run(["aws", "cloudfront", *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def put_page(dry_run: bool) -> None:
    """The body, on CloudFerro, with the credentials from the environment."""
    endpoint = os.environ.get("BASEMAP_S3_ENDPOINT", "https://s3.waw3-1.cloudferro.com")
    dest = f"s3://oceanstream-bucket/basemap{PAGE}"
    if dry_run:
        print(f"  would put {dest}")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(BODY)
    subprocess.run(["aws", "--endpoint-url", endpoint, "s3", "cp", "--only-show-errors", f.name,
                    dest, "--content-type", "text/plain; charset=utf-8",
                    "--cache-control", f"public, max-age={CACHE_S}"], check=True)
    os.unlink(f.name)
    print(f"  put {dest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out = json.loads(aws("get-distribution-config", "--id", DIST))
    config, etag = out["DistributionConfig"], out["ETag"]
    current = config.get("CustomErrorResponses", {"Quantity": 0})
    print(f"error responses now: {json.dumps(current)}")
    if args.show:
        return 0
    put_page(args.dry_run)
    if current == WANTED:
        print("  already set")
        return 0
    if args.dry_run:
        print(f"  would set: {json.dumps(WANTED)}")
        return 0
    config["CustomErrorResponses"] = WANTED
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
    aws("update-distribution", "--id", DIST, "--if-match", etag,
        "--distribution-config", f"file://{f.name}")
    os.unlink(f.name)
    print("  updated; CloudFront is deploying")
    return 0


if __name__ == "__main__":
    sys.exit(main())
