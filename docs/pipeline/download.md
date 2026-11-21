# 00 — Download GEBCO

Downloads the 8 quarter-hemisphere GEBCO GeoTIFF tiles from the CEDA archive and merges them
into a single global DEM. The release year comes from `BASEMAP_GEBCO_RELEASE` (default: 2026).

## Command

```bash
oceanbase run download
```

## Process

1. **Download** 8 tiles from CEDA (~933 MB each, ~7.5 GB total)
2. **Verify** each tile against its published MD5, or against the size the server reports when
   no checksum manifest exists (CEDA publishes none for 2026)
3. **Merge** into a single `GEBCO_2026.tif` using `gdal_merge.py`
4. **Overviews**, at 2 to 128, into `GEBCO_2026.tif.ovr` beside it. Every step that reads
   GEBCO below its own resolution reads them. A merged grid that is already there gets
   them too, so running the step again fills them in.

## Input

| Source | URL |
|--------|-----|
| CEDA Archive | `https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/geotiff/` |

The 8 tiles cover the globe in 4 east–west × 2 north–south slices:

| Tile | Latitude | Longitude |
|------|----------|-----------|
| `gebco_2026_n90.0_s0.0_w-180.0_e-90.0_geotiff.tif` | 0°–90° N | 180°–90° W |
| `gebco_2026_n90.0_s0.0_w-90.0_e0.0_geotiff.tif` | 0°–90° N | 90°–0° W |
| `gebco_2026_n90.0_s0.0_w0.0_e90.0_geotiff.tif` | 0°–90° N | 0°–90° E |
| `gebco_2026_n90.0_s0.0_w90.0_e180.0_geotiff.tif` | 0°–90° N | 90°–180° E |
| `gebco_2026_n0.0_s-90.0_w-180.0_e-90.0_geotiff.tif` | 90°–0° S | 180°–90° W |
| `gebco_2026_n0.0_s-90.0_w-90.0_e0.0_geotiff.tif` | 90°–0° S | 90°–0° W |
| `gebco_2026_n0.0_s-90.0_w0.0_e90.0_geotiff.tif` | 90°–0° S | 0°–90° E |
| `gebco_2026_n0.0_s-90.0_w90.0_e180.0_geotiff.tif` | 90°–0° S | 90°–180° E |

!!! note "Filename convention changed in 2026"
    Releases up to 2025 used `gebco_<year>_<bbox>.tif`; from 2026 CEDA appends a `_geotiff` suffix.

## Output

| File | Description |
|------|-------------|
| `gebco2026/GEBCO_2026.tif` | Merged global DEM (~3 GB) |
| `gebco2026/GEBCO_2026.tif.ovr` | Its overviews, built under a temporary name and renamed when complete |

## Tools

- **httpx** — HTTP downloads with progress bar and checksum verification
- **gdal_merge.py** — Merges multiple GeoTIFFs into one
