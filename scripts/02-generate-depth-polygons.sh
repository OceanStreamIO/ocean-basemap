#!/usr/bin/env bash
# 02-generate-depth-polygons.sh — Create vector depth zone polygons from GEBCO
#
# This is the KEY to not looking "washed out":
# Instead of blending color into the hillshade raster (which degrades quality),
# we generate vector depth-zone polygons and color them client-side in MapLibre.
#
# Same approach as Mapbox Bathymetry v2:
#   - Convert GEBCO elevation grid → classified depth zones
#   - Polygonize each zone → GeoJSON
#   - Convert to PMTiles with tippecanoe
#   - MapLibre fill-color expression based on min_depth property
#
# Result: Pixel-perfect depth coloring at any zoom, no raster blending artifacts.

set -euo pipefail

BASEMAP_DIR="/Volumes/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work"
OUTPUT_DIR="${BASEMAP_DIR}/gebco2025/tiles"

# Use GEBCO 2025 if available, fall back to 2024
if [[ -f "${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif"
elif [[ -f "${BASEMAP_DIR}/work/gebco_2024.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/work/gebco_2024.tif"
else
    echo "ERROR: No GEBCO GeoTIFF found. Run 00-download-gebco2025.sh first."
    exit 1
fi

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"

# Depth zone boundaries (meters) — matches the color ramp in OceanBasemapStyle.ts
# More zones in shallow water (0 to -500m) where visual detail matters most
DEPTH_BREAKS="-11000 -8000 -6000 -5000 -4000 -3000 -2000 -1500 -1000 -500 -200 -100 -50 -10 0"

echo "============================================="
echo "  Step 1: Classify GEBCO into Depth Zones"
echo "============================================="

CLASSIFIED="${WORK_DIR}/gebco_depth_zones.tif"
if [[ -f "$CLASSIFIED" ]]; then
    echo "Classified raster already exists, skipping."
else
    echo "Reclassifying GEBCO into depth zones..."
    # Create a Python reclassification script
    cat > "${WORK_DIR}/reclassify.py" << 'PYEOF'
import numpy as np
import sys

# This is used by gdal_calc.py via --calc parameter
# We use a separate classify step instead
from osgeo import gdal

src = sys.argv[1]
dst = sys.argv[2]

ds = gdal.Open(src)
band = ds.GetRasterBand(1)

# Read in blocks for memory efficiency
xsize = ds.RasterXSize
ysize = ds.RasterYSize

drv = gdal.GetDriverByName('GTiff')
out_ds = drv.Create(dst, xsize, ysize, 1, gdal.GDT_Int16,
                    options=['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=YES', 'NUM_THREADS=ALL_CPUS'])
out_ds.SetGeoTransform(ds.GetGeoTransform())
out_ds.SetProjection(ds.GetProjection())
out_band = out_ds.GetRasterBand(1)
out_band.SetNoDataValue(-32768)

# Depth zone breaks — each pixel gets classified to its zone's minimum depth
breaks = [-11000, -8000, -6000, -5000, -4000, -3000, -2000, -1500, -1000, -500, -200, -100, -50, -10, 0]

block_size = 256
total_blocks = (ysize // block_size + 1) * (xsize // block_size + 1)
done = 0

for y in range(0, ysize, block_size):
    rows = min(block_size, ysize - y)
    for x in range(0, xsize, block_size):
        cols = min(block_size, xsize - x)
        data = band.ReadAsArray(x, y, cols, rows).astype(np.float32)
        result = np.full_like(data, -32768, dtype=np.int16)

        # Classify: each pixel gets the zone's lower bound
        # Land (>= 0) gets value 1 (will be masked in MapLibre)
        result[data >= 0] = 1
        for i in range(len(breaks) - 1):
            mask = (data >= breaks[i]) & (data < breaks[i + 1])
            result[mask] = breaks[i]
        result[data < breaks[0]] = breaks[0]

        out_band.WriteArray(result, x, y)
        done += 1
        if done % 500 == 0:
            pct = int(done / total_blocks * 100)
            print(f"  {pct}% ({done}/{total_blocks} blocks)", flush=True)

out_band.FlushCache()
out_ds = None
ds = None
print("Classification complete.")
PYEOF

    # Use the same Python that GDAL scripts use (Homebrew Python with osgeo bindings)
    GDAL_PYTHON="$(head -1 "$(which gdal_calc.py)" | sed 's/^#!//')"
    echo "Using Python: $GDAL_PYTHON"
    "$GDAL_PYTHON" "${WORK_DIR}/reclassify.py" "$GEBCO_TIF" "$CLASSIFIED"
    echo "Done: $CLASSIFIED"
fi

echo ""
echo "============================================="
echo "  Step 2: Polygonize Depth Zones"
echo "============================================="

POLYGONS="${WORK_DIR}/depth_zones.fgb"
if [[ -f "$POLYGONS" ]]; then
    echo "Polygons already exist, skipping."
else
    echo "Polygonizing depth zones (this takes ~30-60 min for global data)..."
    # Downsample to ~220m for sharp edges in narrow features (fjords, straits, islands)
    DOWNSAMPLED="${WORK_DIR}/gebco_depth_zones_220m.tif"
    if [[ ! -f "$DOWNSAMPLED" ]]; then
        gdalwarp "$CLASSIFIED" "$DOWNSAMPLED" \
            -tr 0.002 0.002 \
            -r near \
            -co COMPRESS=LZW \
            -co TILED=YES \
            -co BIGTIFF=YES \
            -overwrite
    fi

    # Use FlatGeobuf — handles large polygon sets much better than GeoJSON
    RAW_POLYGONS="${WORK_DIR}/depth_zones_raw.fgb"
    echo "Polygonizing downsampled raster..."
    gdal_polygonize.py "$DOWNSAMPLED" \
        -of FlatGeobuf \
        "$RAW_POLYGONS" \
        depth_zones \
        min_depth \
        -q

    # Remove land polygons (min_depth == 1) and nodata (min_depth == -32768)
    echo "Filtering ocean-only polygons..."
    ogr2ogr "$POLYGONS" "$RAW_POLYGONS" \
        -where "min_depth < 0 AND min_depth > -32768"

    rm -f "$RAW_POLYGONS"
    echo "Done: $POLYGONS"
fi

echo ""
echo "============================================="
echo "  Step 3: Generate PMTiles"
echo "============================================="

PMTILES="${OUTPUT_DIR}/depth_zones.pmtiles"
if [[ -f "$PMTILES" ]]; then
    echo "PMTiles already exists, skipping."
else
    echo "Generating PMTiles with tippecanoe..."
    tippecanoe \
        -o "$PMTILES" \
        --minimum-zoom=0 \
        --maximum-zoom=10 \
        --simplification=2 \
        --no-feature-limit \
        --no-tile-size-limit \
        --coalesce-densest-as-needed \
        --detect-shared-borders \
        -l depth_zones \
        --force \
        "$POLYGONS"
    echo "Done: $PMTILES"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "Classified raster: $CLASSIFIED"
echo "Polygons:          $POLYGONS"
echo "PMTiles:           $PMTILES"
if [[ -f "$PMTILES" ]]; then
    ls -lh "$PMTILES"
fi
echo ""
echo "Next: run 03-generate-contours.sh"
