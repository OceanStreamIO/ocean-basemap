# 01 — Hillshade

Generates grayscale hillshade raster tiles from the merged GEBCO DEM. The hillshade provides
3D relief texture that is rendered underneath the vector depth polygons.

## Command

```bash
oceanbase run hillshade
```

## Process

1. **Multi-directional hillshade** via `gdaldem hillshade` with z-factor 8
2. **Gamma correction** (γ = 0.55) to brighten deep-ocean relief using `gdal_calc.py`
3. **Tile generation** — PNG tiles at z0–10 using `gdal2tiles.py` with Lanczos resampling

### Why Gamma Correction?

Raw hillshade output tends to be very dark in deep-ocean areas. Applying a power curve
(`255 × (A / 255)^0.55`) lifts the mid-tones while preserving highlights, making the relief
visible across all depth ranges.

## Input

| File | Description |
|------|-------------|
| `gebco2026/GEBCO_2026.tif` | Merged GEBCO DEM |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/hillshade_multi.tif` | Raw multi-directional hillshade |
| `work/hillshade_gamma.tif` | Gamma-corrected hillshade |

## Output

| Path | Description |
|------|-------------|
| `tiles/hillshade/{z}/{x}/{y}.png` | PNG tile tree, z0–10 |

## GDAL Commands (equivalent)

```bash
# Step 1: Multi-directional hillshade
gdaldem hillshade -z 8 -multidirectional GEBCO_2026.tif hillshade_multi.tif

# Step 2: Gamma correction
gdal_calc.py -A hillshade_multi.tif --outfile=hillshade_gamma.tif \
  --calc="255 * ((A / 255.0) ** 0.55)" --type=Byte

# Step 3: Tiles
gdal2tiles.py -z 0-10 --processes=8 -r lanczos hillshade_gamma.tif tiles/hillshade/
```
