"""Fetch the Copernicus DEM tiles the land relief is rendered from.

GEBCO's land is a 463 m grid, which is what made the relief blur and step at
z10. Copernicus GLO-30 (30 m) covers the European extent the bathymetry is
detailed in, and GLO-90 (90 m) the rest of the world; both are open COGs in
public buckets, one file per 1-degree cell.

There are ~29,500 files, so the fetch runs in parallel threads and keeps the
console to one progress bar. A file is only ever complete on disk: it is
written to ``.part`` and renamed at the end, so a rerun skips what it finds.
"""

from __future__ import annotations

import random
import re
import shutil
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress

console = Console()

Bbox = tuple[float, float, float, float]

_CELL_RE = re.compile(r"_([NS])(\d+)_00_([EW])(\d+)_00")


@dataclass(frozen=True)
class Product:
    """One Copernicus DEM product, as published in its public bucket."""

    name: str
    bucket: str

    @property
    def base(self) -> str:
        return f"https://{self.bucket}.s3.amazonaws.com"

    def url(self, tile: str) -> str:
        """*tile* is a tileList.txt entry, which already ends in ``_DEM``."""
        return f"{self.base}/{tile}/{tile}.tif"


GLO30 = Product("GLO-30", "copernicus-dem-30m")
GLO90 = Product("GLO-90", "copernicus-dem-90m")


def cell(tile: str) -> tuple[int, int]:
    """South-west corner (lon, lat) of a tile, from its name."""
    m = _CELL_RE.search(tile)
    if not m:
        raise ValueError(f"not a Copernicus tile name: {tile}")
    lat = int(m[2]) * (1 if m[1] == "N" else -1)
    lon = int(m[4]) * (1 if m[3] == "E" else -1)
    return lon, lat


def inside(tile: str, extent: Bbox) -> bool:
    lon, lat = cell(tile)
    west, south, east, north = extent
    return west <= lon < east and south <= lat < north


def tile_list(product: Product, client: httpx.Client, cache_dir: Path | None = None) -> list[str]:
    """Every cell name the product publishes, from *cache_dir* or the bucket.

    Cached on disk because this one small file gates the whole step: the tiles
    themselves are skipped when they are already there, so without a cache a
    host with 135 GB of DEM staged and no internet still fails on the very
    first call. The list changes only when Copernicus republishes the product,
    so a stale cache is not a real risk; delete the file to refresh it.
    """
    cached = cache_dir / f"{product.name}.tileList.txt" if cache_dir else None
    if cached is not None and cached.is_file():
        return [line.strip() for line in cached.read_text().splitlines() if line.strip()]

    from basemap.config import OFFLINE
    from basemap.utils.download import Missing

    if OFFLINE:
        raise Missing(
            f"{product.name}: no cached tile list and BASEMAP_OFFLINE is set.\n"
            f"  wanted: {cached}\n  from:   {product.base}/tileList.txt\n"
            "  Stage it with the build inputs and run again."
        )

    r = client.get(f"{product.base}/tileList.txt")
    r.raise_for_status()
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(r.text)
    return [line.strip() for line in r.text.splitlines() if line.strip()]


def select(
    extent: Bbox, client: httpx.Client, cache_dir: Path | None = None
) -> dict[Product, list[str]]:
    """GLO-30 inside *extent*, GLO-90 everywhere else."""
    return {
        GLO30: [t for t in tile_list(GLO30, client, cache_dir) if inside(t, extent)],
        GLO90: [t for t in tile_list(GLO90, client, cache_dir) if not inside(t, extent)],
    }


def projected_bytes(
    product: Product, tiles: Sequence[str], client: httpx.Client, sample: int = 60
) -> int:
    """Estimate the download from the sizes of a random sample of tiles."""
    picked = random.Random(0).sample(list(tiles), min(sample, len(tiles)))
    sizes = []
    for tile in picked:
        r = client.head(product.url(tile))
        if r.status_code == 200:
            sizes.append(int(r.headers.get("content-length", 0)))
    return int(sum(sizes) / max(len(sizes), 1) * len(tiles))


def _fetch(product: Product, tile: str, dest_dir: Path, client: httpx.Client) -> Path:
    dest = dest_dir / f"{tile}.tif"
    if dest.is_file():
        return dest
    partial = dest.with_suffix(".part")
    with client.stream("GET", product.url(tile)) as r:
        r.raise_for_status()
        with open(partial, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
    partial.replace(dest)
    return dest


def fetch(product: Product, tiles: Sequence[str], dest_dir: Path, workers: int = 16) -> list[Path]:
    """Download *tiles* into *dest_dir*, skipping any already there."""
    from basemap.config import OFFLINE
    from basemap.utils.download import Missing

    dest_dir.mkdir(parents=True, exist_ok=True)
    todo = [t for t in tiles if not (dest_dir / f"{t}.tif").is_file()]
    console.print(
        f"  {product.name}: {len(tiles)} tiles, {len(tiles) - len(todo)} already on disk"
    )
    # One report naming the count and a sample, rather than sixteen threads each
    # discovering the same thing one 300 s timeout at a time.
    if todo and OFFLINE:
        raise Missing(
            f"{product.name}: {len(todo)} of {len(tiles)} tiles are missing and "
            f"BASEMAP_OFFLINE is set.\n  wanted in: {dest_dir}\n"
            f"  first few: {', '.join(todo[:3])}\n"
            "  Stage the DEM with the build inputs and run again."
        )
    with (
        httpx.Client(timeout=300, follow_redirects=True) as client,
        ThreadPoolExecutor(max_workers=workers) as pool,
        Progress(console=console) as progress,
    ):
        task = progress.add_task(f"{product.name}", total=len(todo))
        futures = [pool.submit(_fetch, product, t, dest_dir, client) for t in todo]
        for future in futures:
            future.result()
            progress.advance(task)
    return sorted(dest_dir.glob("*_DEM.tif"))


def require_space(path: Path, needed: int, margin: int = 30 * 1024**3) -> None:
    """Stop before a download that would fill the disk."""
    free = shutil.disk_usage(path).free
    if free < needed + margin:
        console.print(
            f"[red bold]ERROR:[/] the DEM download needs ~{needed / 1024**3:.0f} GB plus a "
            f"{margin / 1024**3:.0f} GB margin, but only {free / 1024**3:.0f} GB is free "
            f"at {path}."
        )
        raise SystemExit(1)
