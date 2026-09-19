#!/usr/bin/env python3
"""Derive depth and elevation → colour ramps from the EMODnet World Base Layer.

app.oceanstream.io draws the EMODnet base layer, so the custom basemap borrows
its palette to look like the same product. EMODnet publishes the rendered
tiles, not the ramp behind them, so this recovers it: fetch base-layer tiles,
read the elevation of the same pixels, and take the median colour in each
depth bin. The median also discards most of the hillshading the base layer
bakes in, which pushes pixels lighter or darker around the true ramp colour.

The land is sampled the same way. EMODnet's shallows fade to near-white, and
its warmth is on the land from the low hills up, so taking only the sea ramp
would miss the look.

The raw medians go to color-ramps/sampled/. The ramps the build reads,
color-ramps/bathymetry-emodnet.txt and land-emodnet.txt, are curated from them
by hand (see their headers), so builds never touch the network.

    uv run python scripts/sample-emodnet-palette.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from basemap.config import EMODNET_EXTENT, get_data_dir, get_gebco_tif, get_work_dir
from basemap.processing.raster_tiles import Source, read_elevation, tiles_in_bbox

URL = "https://tiles.emodnet-bathymetry.eu/2020/baselayer/web_mercator/{z}/{x}/{y}.png"
ZOOM = 8
# Denser near the surface, where the ramp changes fastest and matters most.
BINS = [
    0, -2, -5, -10, -15, -20, -30, -40, -50, -75, -100, -150, -200, -300, -400, -500,
    -750, -1000, -1500, -2000, -2500, -3000, -3500, -4000, -4500, -5000, -5500, -6000,
    -7000, -8000, -9000, -11000,
]
LAND_BINS = [
    0, 10, 25, 50, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 2500, 3000,
    3500, 4000, 5000, 6500,
]
# Beyond EMODnet's seas: trenches for the deep sea, ranges for high ground.
DEEP = [(142.2, 11.3), (-66.5, 19.8), (-175.0, 51.0), (154.0, 44.0), (-72.0, -25.0)]
HIGH = [(86.5, 28.0), (-70.0, -20.0), (-106.0, 39.0), (75.0, 36.0)]
MIN_PIXELS = 200


def _tiles() -> list[tuple[int, int]]:
    grid = list(tiles_in_bbox(EMODNET_EXTENT, ZOOM))
    picked = grid[:: max(1, len(grid) // 90)]
    for lon, lat in DEEP + HIGH:
        picked += list(tiles_in_bbox((lon - 1, lat - 1, lon + 1, lat + 1), ZOOM))
    return picked


def _ramp(value, colour, bins, *, below: bool) -> list[tuple[float, np.ndarray, int]]:
    """Median colour per bin; bins run away from zero in either direction."""
    stops = []
    for a, b in zip(bins, bins[1:]):
        inside = ((value <= a) & (value > b)) if below else ((value >= a) & (value < b))
        if inside.sum() >= MIN_PIXELS:
            rgb = np.median(colour[inside], axis=0).round().astype(int)
            stops.append(((a + b) / 2, rgb, int(inside.sum())))
    return stops


def _write(name: str, title: str, stops) -> None:
    out = Path(__file__).resolve().parent.parent / "color-ramps" / "sampled" / name
    lines = [
        f"# {title} sampled from the EMODnet World Base Layer",
        "# (https://emodnet.ec.europa.eu/en/bathymetry), by scripts/sample-emodnet-palette.py:",
        "# median base-layer colour per elevation bin, against the EMODnet DTM and GEBCO.",
        "# Format: elevation  R  G  B   (gdaldem color-relief)",
    ]
    lines += [f"{d:<8g} {r:3d} {g:3d} {b:3d}   # {n} px" for d, (r, g, b), n in stops]
    out.write_text("\n".join(lines) + "\n")
    print(out.read_text())


def main() -> int:
    base = get_data_dir(None)
    sources = (
        Source(str(get_work_dir(base) / "emodnet" / "emodnet_mean.vrt")),
        Source(str(get_gebco_tif(base))),
    )
    values, colours = [], []
    with httpx.Client(timeout=60) as client:
        for x, y in _tiles():
            r = client.get(URL.format(z=ZOOM, x=x, y=y))
            if r.status_code != 200:
                continue
            rgb = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), dtype=np.float32)
            elev = read_elevation(sources, ZOOM, x, y, buffer=0)
            ok = np.isfinite(elev)
            values.append(elev[ok])
            colours.append(rgb[ok])
    value = np.concatenate(values)
    colour = np.concatenate(colours)
    print(f"sampled {value.size:,} pixels", file=sys.stderr)

    _write("bathymetry-emodnet.txt", "Bathymetry ramp",
           _ramp(value, colour, BINS, below=True))
    _write("land-emodnet.txt", "Land ramp",
           _ramp(value, colour, LAND_BINS, below=False))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("BASEMAP_DATA_DIR", "/home/onyxia/work/basemap-data")
    sys.exit(main())
