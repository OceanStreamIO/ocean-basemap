#!/usr/bin/env python3
"""List the elevation cells the build actually needs, for rclone --files-from.

The two products' buckets hold about 26,000 one-degree cells each, and the
pipeline wants a different subset of each: the fine product only inside the
extent the regional bathymetry covers, the coarse one everywhere else. Pulling
both buckets whole is not a small waste -- the fine cells are about 27 MB each,
so its bucket is roughly 720 GB against the 83 GB actually used, and it would
fill the staging quota to pull data that is then ignored.

The selection rule is not reimplemented here. It is the pipeline's own
``copernicus.select``, so the staged set cannot drift from the set the build
reads.

    ./deploy/hpc/dem_manifest.py --out-dir /tmp        # writes glo30.files, glo90.files

Paths are relative to the bucket root and name the cell directory as well as the
file inside it, because that is how the bucket is laid out.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

PRODUCTS = {"glo30": "GLO-30", "glo90": "GLO-90"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("/tmp"))
    parser.add_argument("--cache-dir", type=pathlib.Path,
                        help="where the tile lists are cached (default: alongside the output)")
    args = parser.parse_args()

    import httpx

    from basemap.config import EMODNET_EXTENT
    from basemap.processing import copernicus

    cache = args.cache_dir or args.out_dir
    cache.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        selected = copernicus.select(EMODNET_EXTENT, client, cache)

    by_name = {product.name: tiles for product, tiles in selected.items()}
    for subdir, name in PRODUCTS.items():
        tiles = by_name[name]
        listing = args.out_dir / f"{subdir}.files"
        listing.write_text("".join(f"{tile}/{tile}.tif\n" for tile in sorted(tiles)))
        total = len(copernicus.tile_list(
            next(p for p in selected if p.name == name), client=None, cache_dir=cache))
        print(f"{subdir}: {len(tiles):,} of {total:,} cells -> {listing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
