# 02 — Depth Polygons

Reclassifies the GEBCO DEM into 14 depth zones, polygonizes the result, and generates vector
PMTiles. These polygons carry the crisp depth coloring that overlays the hillshade.

## Command

```bash
oceanbase run depth-polygons
```

## Process

1. **Reclassify** — Assign each pixel to one of 14 depth zones using rasterio (windowed I/O)
2. **Downsample** to ~220 m resolution with nearest-neighbor resampling
3. **Polygonize** — Convert raster zones to vector polygons via `gdal_polygonize.py`
4. **Filter** — Remove land (≥ 0) and nodata pixels
5. **Generate PMTiles** via tippecanoe with shared border detection

### Depth Zones

| Zone | Depth Range | Color |
|------|-------------|-------|
| 1 | −11 000 to −8 000 m | <span class="depth-swatch" style="background:#080c2e"></span> `#080c2e` |
| 2 | −8 000 to −6 000 m | <span class="depth-swatch" style="background:#0a1944"></span> `#0a1944` |
| 3 | −6 000 to −5 000 m | <span class="depth-swatch" style="background:#0e285a"></span> `#0e285a` |
| 4 | −5 000 to −4 000 m | <span class="depth-swatch" style="background:#12346c"></span> `#12346c` |
| 5 | −4 000 to −3 000 m | <span class="depth-swatch" style="background:#19427e"></span> `#19427e` |
| 6 | −3 000 to −2 000 m | <span class="depth-swatch" style="background:#245494"></span> `#245494` |
| 7 | −2 000 to −1 500 m | <span class="depth-swatch" style="background:#346cac"></span> `#346cac` |
| 8 | −1 500 to −1 000 m | <span class="depth-swatch" style="background:#4480bc"></span> `#4480bc` |
| 9 | −1 000 to −500 m | <span class="depth-swatch" style="background:#5898cc"></span> `#5898cc` |
| 10 | −500 to −200 m | <span class="depth-swatch" style="background:#70b0da"></span> `#70b0da` |
| 11 | −200 to −100 m | <span class="depth-swatch" style="background:#8cc4e6"></span> `#8cc4e6` |
| 12 | −100 to −50 m | <span class="depth-swatch" style="background:#a8d6f0"></span> `#a8d6f0` |
| 13 | −50 to −10 m | <span class="depth-swatch" style="background:#c0e4f8"></span> `#c0e4f8` |
| 14 | −10 to 0 m | <span class="depth-swatch" style="background:#d2eefb"></span> `#d2eefb` |

## Input

| File | Description |
|------|-------------|
| `gebco2026/GEBCO_2026.tif` | Merged GEBCO DEM |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/gebco_depth_zones.tif` | Reclassified raster (zone IDs) |
| `work/gebco_depth_zones_220m.tif` | Downsampled raster |
| `work/depth_zones.fgb` | Filtered FlatGeobuf polygons |

## Output

| File | Description |
|------|-------------|
| `tiles/depth_zones.pmtiles` | Vector PMTiles, z0–10 |

## Tippecanoe Options

```
--simplification=2
--no-feature-limit
--no-tile-size-limit
--detect-shared-borders
```
