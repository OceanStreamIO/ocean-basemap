"""Pipeline step 06u (S3): Upload generated tiles to an S3-compatible bucket.

Targets CloudFerro / EDITO object storage by default, but works with any
S3 API endpoint. Credentials are read from the environment:

    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (standard, preferred)
    S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY     (EDITO datalab naming)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from basemap.config import (
    S3_BUCKET,
    S3_CACHE_CONTROL_PMTILES,
    S3_CACHE_CONTROL_TILES,
    S3_ENDPOINT,
    S3_PREFIX,
    S3_PUBLIC_BASE,
    S3_REGION,
    get_data_dir,
    get_tile_dir,
)

console = Console()

PMTILES_CONTENT_TYPE = "application/vnd.pmtiles"


def _require_aws() -> str:
    path = shutil.which("aws")
    if not path:
        console.print("[red bold]ERROR:[/] AWS CLI not found.")
        console.print("[dim]Install with: uv tool install awscli  (or: brew install awscli)[/dim]")
        raise SystemExit(1)
    return path


def _credential_env() -> dict[str, str]:
    """Environment for the aws CLI, accepting either AWS_* or S3_* key names."""
    env = os.environ.copy()
    env.setdefault("AWS_ACCESS_KEY_ID", env.get("S3_ACCESS_KEY_ID", ""))
    env.setdefault("AWS_SECRET_ACCESS_KEY", env.get("S3_SECRET_ACCESS_KEY", ""))
    env.setdefault("AWS_DEFAULT_REGION", S3_REGION)
    # These buckets are not on EC2; skip the metadata probe that otherwise stalls.
    env["AWS_EC2_METADATA_DISABLED"] = "true"

    if not env["AWS_ACCESS_KEY_ID"] or not env["AWS_SECRET_ACCESS_KEY"]:
        console.print("[red bold]ERROR:[/] No S3 credentials in environment.")
        console.print("[dim]Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY[/dim]")
        raise SystemExit(1)
    return env


def _key(*parts: str) -> str:
    return "/".join(p for p in (S3_PREFIX, *parts) if p)


def _run_aws(args: list[str], *, label: str, env: dict[str, str]) -> None:
    console.print(f"  [cyan]{label}[/cyan]")
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        console.print(f"[red]aws command failed:[/red] {result.stderr.strip()[:300]}")
        raise RuntimeError(f"aws CLI failed with exit code {result.returncode}")


def run(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 06u: Upload to S3[/bold]\n")

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)
    aws = _require_aws()
    env = _credential_env()
    endpoint = ["--endpoint-url", S3_ENDPOINT]

    console.print(f"  [dim]endpoint {S3_ENDPOINT} · bucket {S3_BUCKET} · prefix {S3_PREFIX}/[/dim]")

    # Raster tiles — large trees of small PNGs, so sync in bulk.
    for name in ("hillshade", "hypsometric"):
        raster_dir = tile_dir / name
        if (raster_dir / "0").exists():
            _run_aws(
                [aws, *endpoint, "s3", "sync", str(raster_dir),
                 f"s3://{S3_BUCKET}/{_key(name)}",
                 "--exclude", "*", "--include", "*.png",
                 "--content-type", "image/png",
                 "--cache-control", S3_CACHE_CONTROL_TILES,
                 "--only-show-errors"],
                label=f"Sync {name} tiles",
                env=env,
            )
        else:
            console.print(f"  [dim]No {name} tiles, skipping.[/dim]")

    for pmtile_file in sorted(tile_dir.glob("*.pmtiles")):
        _run_aws(
            [aws, *endpoint, "s3", "cp", str(pmtile_file),
             f"s3://{S3_BUCKET}/{_key('tiles', pmtile_file.name)}",
             "--content-type", PMTILES_CONTENT_TYPE,
             "--cache-control", S3_CACHE_CONTROL_PMTILES,
             "--only-show-errors"],
            label=f"Upload {pmtile_file.name}",
            env=env,
        )

    console.print(f"\n[green]Uploaded to:[/green] {S3_PUBLIC_BASE}/")
