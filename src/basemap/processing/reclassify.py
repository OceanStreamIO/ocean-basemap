"""Reclassify GEBCO elevation grid into depth zone classes.

Uses rasterio for efficient block-level reads of the global GEBCO raster.
Each pixel gets assigned the lower-bound value of its depth zone.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rich.console import Console
from rich.progress import Progress

from basemap.config import DEPTH_BREAKS

console = Console()


def reclassify_depth_zones(
    src: Path,
    dst: Path,
    *,
    breaks: list[int] | None = None,
    block_size: int = 256,
) -> Path:
    """Classify elevation raster into depth zones.

    Each ocean pixel gets the zone's lower-bound value.
    Land pixels (>= 0) get value 1.
    NoData pixels get -32768.
    """
    breaks = breaks or DEPTH_BREAKS

    with rasterio.open(src) as ds:
        profile = ds.profile.copy()
        profile.update(
            dtype="int16",
            compress="lzw",
            bigtiff="YES",
            nodata=-32768,
            count=1,
        )
        # Only use tiling if the raster is large enough (multiples of 16)
        if ds.width >= 256 and ds.height >= 256:
            profile.update(tiled=True)
        else:
            profile.pop("tiled", None)
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)

        with rasterio.open(dst, "w", **profile) as out_ds:
            total_blocks = 0
            for _ in ds.block_windows(1):
                total_blocks += 1

            with Progress(console=console) as progress:
                task = progress.add_task("Reclassifying depth zones", total=total_blocks)

                for _, window in ds.block_windows(1):
                    data = ds.read(1, window=window).astype(np.float32)
                    result = np.full(data.shape, -32768, dtype=np.int16)

                    # Land
                    result[data >= 0] = 1

                    # Ocean depth zones
                    for i in range(len(breaks) - 1):
                        mask = (data >= breaks[i]) & (data < breaks[i + 1])
                        result[mask] = breaks[i]

                    # Below deepest break
                    result[data < breaks[0]] = breaks[0]

                    out_ds.write(result, 1, window=window)
                    progress.advance(task)

    console.print(f"  [green]Classified[/green]: {dst.name}")
    return dst
