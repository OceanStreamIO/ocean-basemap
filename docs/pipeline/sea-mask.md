# 05b — Sea Mask

Builds the complement of the OSM land polygons and tiles it as `sea_mask.pmtiles`.

## Command

```bash
oceanbase run sea-mask
```

MapLibre cannot clip a raster to a polygon, so the land relief and the satellite imagery
are hidden at sea by this vector fill. Its edge is the shoreline everywhere, antialiased
by the GPU at any zoom.

Only 2° cells that contain land are built. Open ocean has no shoreline to draw, and the
bathymetry tiles cover it anyway. The cell pass resumes after an interruption, and a
failed cell is retried rather than skipped.
