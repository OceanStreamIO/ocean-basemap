# 06 — Hypsometric

Generates land-only color relief tiles using an EMODnet-style hypsometric color ramp. Ocean areas
are rendered as transparent.

## Command

```bash
oceanbase run hypsometric
```

## Process

1. **Write color ramp** file from the built-in EMODnet-style configuration
2. **Create land-only DEM** — set ocean pixels (elevation < 0) to nodata
3. **Apply color relief** via `gdaldem color-relief` with alpha band (transparent ocean)
4. **Generate PNG tiles** z0–8 with Lanczos resampling

### Color Ramp

The hypsometric ramp maps elevation to natural-looking land colors:

| Elevation | Color | Description |
|-----------|-------|-------------|
| −1 m | rgb(170, 210, 220) | Coastal lowland |
| 0–50 m | rgb(200, 220, 160) | Green lowland |
| 100–300 m | rgb(215, 215, 140) | Low hills |
| 500–1 000 m | rgb(210, 175, 115) | Highland |
| 1 500–2 500 m | rgb(180, 140, 105) | Mountain |
| 3 000–5 000 m | rgb(200, 195, 185) | High mountain |
| 6 000–9 000 m | rgb(245, 243, 240) | Peak / glacier |

## Input

| File | Description |
|------|-------------|
| `gebco2025/GEBCO_2025.tif` | Merged GEBCO DEM |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/hypsometric/hypsometric_ramp.txt` | GDAL color relief ramp file |
| `work/hypsometric/gebco_land_only.tif` | Land-only DEM (ocean = nodata) |
| `work/hypsometric/hypsometric_land.tif` | RGBA color relief raster |

## Output

| Path | Description |
|------|-------------|
| `tiles/hypsometric/{z}/{x}/{y}.png` | PNG tile tree, z0–8 |

## GDAL Commands (equivalent)

```bash
# Land-only DEM
gdal_calc.py -A GEBCO_2025.tif --outfile=gebco_land_only.tif \
  --calc="numpy.where(A >= 0, A, -9999)" --type=Float32 --NoDataValue=-9999

# Color relief
gdaldem color-relief -alpha gebco_land_only.tif hypsometric_ramp.txt hypsometric_land.tif

# Tile generation
gdal2tiles.py -z 0-8 --processes=8 -r lanczos hypsometric_land.tif tiles/hypsometric/
```
