# 02b — EMODnet Regional Depth Zones

Builds a higher-resolution depth-zone overlay for European seas from the
[EMODnet Bathymetry](https://emodnet.ec.europa.eu/en/bathymetry) DTM (~115 m), layered above the
global GEBCO depth zones at high zoom.

## Command

This step is **opt-in** — `oceanbase run all` skips it unless asked:

```bash
oceanbase run emodnet
oceanbase run all --with emodnet
```

## Process

1. **Download** the DTM through the EMODnet OGC WCS, chunked into 5° requests
2. **Mosaic** the chunks into a VRT (no pixel copy)
3. **Classify** into the same 14 depth zones as the global layer
4. **Polygonize** at ~110 m and keep ocean cells only
5. **Tile** into `depth_zones_eu.pmtiles` at z6–12

## Input

| Source | Value |
|--------|-------|
| WCS endpoint | `https://ows.emodnet-bathymetry.eu/wcs` |
| Coverage | `emodnet__mean` (latest DTM; `emodnet__mean_<year>` for historical releases) |
| Native resolution | 1/16 arc-minute (~115 m) |
| Coverage envelope | 70.5° W – 43° E, 11° N – 90° N |
| Requested extent | 36° W – 43° E, 15° N – 84° N (`EMODNET_EXTENT`) |

The default extent covers the European seas and stops below the Web Mercator limit. The coverage
also includes a Caribbean tile (70.5°–60° W, 11°–19° N) that the default extent excludes.

!!! warning "Vertical datum differs from GEBCO"
    EMODnet depths are referenced to **Lowest Astronomical Tide (LAT)**, while GEBCO uses mean sea
    level. Expect a metre-scale offset against the global layer in macro-tidal areas such as the
    Bristol Channel and the Bay of Fundy. The pipeline does not convert between the two.

!!! note "Gaps are GEBCO"
    Where EMODnet has no survey or composite DTM coverage, its own grid is filled from GEBCO — so
    outside surveyed shelf areas this layer carries no extra detail.

## Output

| File | Description |
|------|-------------|
| `gebco2026/tiles/depth_zones_eu.pmtiles` | Depth zone polygons, layer `depth_zones_eu`, z6–12 |

Intermediates land in `gebco2026/work/emodnet/`: the downloaded `chunks/`, the `emodnet_mean.vrt`
mosaic, and the classified rasters. Chunk downloads are resumable — re-running the step skips
files that are already present.

## Configuration

| Constant | Default | Description |
|----------|---------|-------------|
| `EMODNET_EXTENT` | `(-36, 15, 43, 84)` | Request extent (west, south, east, north) |
| `EMODNET_CHUNK_DEG` | `5.0` | Size of each WCS request; 5° ≈ 4800 × 4800 px |
| `EMODNET_POLYGONIZE_RES` | `0.001` | Degrees per pixel before polygonizing |
| `EMODNET_MIN_ZOOM` / `EMODNET_MAX_ZOOM` | `6` / `12` | PMTiles zoom range |

## Tools

- **httpx** — WCS GetCoverage requests
- **gdalbuildvrt** — Mosaics chunks without duplicating pixel data
- **rasterio** — Depth-zone classification
- **gdalwarp / gdal_polygonize.py / ogr2ogr** — Raster to filtered polygons
- **tippecanoe** — PMTiles output

## Attribution

> EMODnet Digital Bathymetry (DTM 2024). EMODnet Bathymetry Consortium.
> doi:[10.12770/cf51df64-56f9-4a99-b1aa-36b8d7b743a1](https://doi.org/10.12770/cf51df64-56f9-4a99-b1aa-36b8d7b743a1)
