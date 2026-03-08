# 00 — Download GEBCO 2025

Downloads the 8 quarter-hemisphere GEBCO 2025 GeoTIFF tiles from the CEDA archive and merges them
into a single global DEM.

## Command

```bash
oceanbase run download
```

## Process

1. **Download** 8 tiles from CEDA (~890 MB each, ~7.1 GB total)
2. **Verify** MD5 checksums for each tile
3. **Merge** into a single `GEBCO_2025.tif` using `gdal_merge.py`

## Input

| Source | URL |
|--------|-----|
| CEDA Archive | `https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2025/ice_surface_elevation/geotiff/` |

The 8 tiles cover the globe in 4 east–west × 2 north–south slices:

| Tile | Latitude | Longitude |
|------|----------|-----------|
| `gebco_2025_n90.0_s0.0_w-180.0_e-90.0.tif` | 0°–90° N | 180°–90° W |
| `gebco_2025_n90.0_s0.0_w-90.0_e0.0.tif` | 0°–90° N | 90°–0° W |
| `gebco_2025_n90.0_s0.0_w0.0_e90.0.tif` | 0°–90° N | 0°–90° E |
| `gebco_2025_n90.0_s0.0_w90.0_e180.0.tif` | 0°–90° N | 90°–180° E |
| `gebco_2025_n0.0_s-90.0_w-180.0_e-90.0.tif` | 90°–0° S | 180°–90° W |
| `gebco_2025_n0.0_s-90.0_w-90.0_e0.0.tif` | 90°–0° S | 90°–0° W |
| `gebco_2025_n0.0_s-90.0_w0.0_e90.0.tif` | 90°–0° S | 0°–90° E |
| `gebco_2025_n0.0_s-90.0_w90.0_e180.0.tif` | 90°–0° S | 90°–180° E |

## Output

| File | Description |
|------|-------------|
| `gebco2025/GEBCO_2025.tif` | Merged global DEM (~3 GB) |

## Tools

- **httpx** — HTTP downloads with progress bar and checksum verification
- **gdal_merge.py** — Merges multiple GeoTIFFs into one
