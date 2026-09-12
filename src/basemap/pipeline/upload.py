"""Pipeline step 06u: Upload generated tiles to Azure Blob Storage or S3.

The backend is chosen by ``BASEMAP_UPLOAD_TARGET`` (``azure`` or ``s3``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from basemap.config import (
    AZURE_CONTAINER,
    AZURE_STORAGE_ACCOUNT,
    BLOB_BASE,
    UPLOAD_TARGET,
    get_data_dir,
    get_tile_dir,
)

console = Console()


def _require_az() -> str:
    path = shutil.which("az")
    if not path:
        console.print("[red bold]ERROR:[/] Azure CLI not found.")
        console.print("[dim]Install with: brew install azure-cli[/dim]")
        raise SystemExit(1)
    return path


def _run_az(args: list[str], *, label: str) -> None:
    console.print(f"  [cyan]{label}[/cyan]")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]az command failed:[/red] {result.stderr.strip()[:200]}")
        raise RuntimeError(f"az CLI failed with exit code {result.returncode}")


def run(data_dir: Path | None = None) -> None:
    if UPLOAD_TARGET == "s3":
        from basemap.pipeline import upload_s3

        upload_s3.run(data_dir=data_dir)
        return
    if UPLOAD_TARGET != "azure":
        console.print(f"[red]Unknown BASEMAP_UPLOAD_TARGET:[/red] {UPLOAD_TARGET}")
        console.print("[dim]Expected 'azure' or 's3'.[/dim]")
        raise SystemExit(1)
    _run_azure(data_dir)


def _run_azure(data_dir: Path | None = None) -> None:
    console.print("\n[bold]Step 06u: Upload to Azure[/bold]\n")

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)
    az = _require_az()

    # Check Azure login
    check = subprocess.run([az, "account", "show"], capture_output=True, text=True)
    if check.returncode != 0:
        console.print("Not logged in to Azure. Running: az login")
        subprocess.run([az, "login"], check=True)

    # Create container
    _run_az(
        [az, "storage", "container", "create",
         "--name", AZURE_CONTAINER,
         "--account-name", AZURE_STORAGE_ACCOUNT,
         "--public-access", "blob"],
        label="Ensure container exists",
    )

    # Upload hillshade raster tiles
    hillshade_dir = tile_dir / "hillshade"
    if (hillshade_dir / "0").exists():
        _run_az(
            [az, "storage", "blob", "upload-batch",
             "--destination", AZURE_CONTAINER,
             "--source", str(hillshade_dir),
             "--destination-path", "hillshade",
             "--account-name", AZURE_STORAGE_ACCOUNT,
             "--pattern", "*.png",
             "--content-type", "image/png",
             "--overwrite"],
            label="Upload hillshade tiles",
        )
    else:
        console.print("  [dim]No hillshade tiles, skipping.[/dim]")

    # Upload PMTiles
    for pmtile_file in sorted(tile_dir.glob("*.pmtiles")):
        _run_az(
            [az, "storage", "blob", "upload",
             "--container-name", AZURE_CONTAINER,
             "--file", str(pmtile_file),
             "--name", f"tiles/{pmtile_file.name}",
             "--account-name", AZURE_STORAGE_ACCOUNT,
             "--content-type", "application/vnd.pmtiles",
             "--overwrite"],
            label=f"Upload {pmtile_file.name}",
        )

    console.print(f"\n[green]Uploaded to:[/green] {BLOB_BASE}/")
