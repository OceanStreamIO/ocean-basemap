# 05f — Land Cover (ESA WorldCover)

Stages the ESA WorldCover 2021 map at 74 m, which the [land relief](hypsometric.md) reads to tint each pixel by what grows on it. The tint is what gives the plains the variation EMODnet's have; see `processing/landcover.py` for what each class does.

## Command

```bash
oceanbase run worldcover
```

It is part of `oceanbase run all`, before the land relief, which stops without it unless
`BASEMAP_LAND_COVER=off`.

`BASEMAP_RENDER_BBOX` confines it to the tiles touching a box, for a preview. `BASEMAP_WORLDCOVER_WORKERS` sets the parallel fetches (16).

## Process

1. **The grid.** `esa_worldcover_grid.fgb` names the 2,651 three-degree tiles that exist; the ocean has none, so this is the list to fetch rather than every cell on earth.
2. **Three overviews per tile.** Each tile is a 70 MB cloud-optimised GeoTIFF at 0.3 arc-seconds (about 9 m) with internal overviews, and nothing here needs the 10 m pixels: a z12 tile pixel is 38 m. Each tile is read at its third overview (74 m at the equator, 4,500 pixels square) and at two coarser ones (297 m and 593 m) for the low zooms, through `/vsicurl/` so only those bytes travel. The world comes to a few gigabytes under `work/cover/worldcover/`.
3. **Mosaics.** One VRT per level, written by this step rather than by `gdalbuildvrt`, because the build Job's image carries GDAL only inside the rasterio wheel. The tiles share a grid and do not overlap, so a mosaic is a list of placements, with paths relative to the VRT so it travels with its tiles.

The renderer reads the coarsest level still finer than the tile's pixels, nearest-neighbour: a class cannot be averaged.

## Source

| | |
|---|---|
| **Product** | ESA WorldCover 2021, v200 |
| **Resolution** | 0.3 arc-seconds (about 9 m), read at 74 m, 297 m and 593 m |
| **Classes** | tree cover, shrubland, grassland, cropland, built-up, bare, snow and ice, water, herbaceous wetland, mangroves, moss and lichen |
| **Licence** | CC BY 4.0 — credit *© ESA WorldCover project 2022, produced by the WorldCover consortium* |
| **Access** | AWS open data, `s3://esa-worldcover` (`eu-central-1`) |
