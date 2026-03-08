# 03 — Contours

Extracts depth contour lines from the GEBCO DEM at multiple intervals and combines them into a
single multi-layer PMTiles file.

## Command

```bash
oceanbase run contours
```

## Process

1. **Downsample** GEBCO to 1 km and 500 m resolution using cubic spline interpolation (smoother contours)
2. **Extract contours** at four interval levels:
    - **Major** (1 000 m) from the 1 km grid
    - **Minor** (500 m) from the 1 km grid
    - **Shelf** (200 m + fixed levels at −100, −50 m) from the 1 km grid
    - **Shallow** (fixed levels: −150, −100, −75, −50, −40, −30, −20, −10 m) from the 500 m grid
3. **Filter** — Keep ocean-only contours (`depth < 0`)
4. **Generate PMTiles** — Multi-layer with per-level zoom ranges

### Zoom Ranges

| Contour Set | Interval | Min Zoom |
|-------------|----------|----------|
| Major | 1 000 m | 2 |
| Minor | 500 m | 3 |
| Shelf | 200 m | 4 |
| Shallow | 10–150 m | 4 |

## Input

| File | Description |
|------|-------------|
| `gebco2025/GEBCO_2025.tif` | Merged GEBCO DEM |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/contours/gebco_1km.tif` | Downsampled to ~1 km |
| `work/contours/gebco_500m.tif` | Downsampled to ~500 m |
| `work/contours/contours_major.geojson` | 1 000 m contours |
| `work/contours/contours_500m.geojson` | 500 m contours |
| `work/contours/contours_200m.geojson` | 200 m contours |
| `work/contours/contours_shallow.geojson` | Shallow contours |

## Output

| File | Description |
|------|-------------|
| `tiles/depth_contours.pmtiles` | Multi-layer vector PMTiles, z2–10 |

## GDAL Commands (equivalent)

```bash
# Downsample
gdalwarp -tr 0.01 0.01 -r cubicspline GEBCO_2025.tif gebco_1km.tif

# Major contours
gdal_contour -i 1000 gebco_1km.tif contours_major.geojson

# Fixed-level shallow contours
gdal_contour -fl -150 -100 -75 -50 -40 -30 -20 -10 gebco_500m.tif contours_shallow.geojson
```
