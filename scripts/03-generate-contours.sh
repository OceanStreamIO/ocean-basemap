#!/usr/bin/env bash
# 03-generate-contours.sh — Extract depth contours from GEBCO → PMTiles
#
# Generates smooth vector contour lines at multiple depth intervals.
# Lines are simplified to remove jagged artifacts from the grid.
#
# Output properties per feature:
#   - depth: contour depth in meters (negative = ocean)
#   - depth_class: "major" (1000m+), "minor" (200-500m), "shelf" (50-100m)
#   - abs_depth: absolute depth value (for labeling)

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work/contours"
OUTPUT_DIR="${BASEMAP_DIR}/gebco2025/tiles"

if [[ -f "${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif"
elif [[ -f "${BASEMAP_DIR}/work/gebco_2024.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/work/gebco_2024.tif"
else
    echo "ERROR: No GEBCO GeoTIFF found. Run 00-download-gebco2025.sh first."
    exit 1
fi

mkdir -p "$WORK_DIR"

echo "============================================="
echo "  Step 1: Downsample for Contour Smoothness"
echo "============================================="

# Two resolutions: coarse for deep ocean, fine for shelf detail
# ~1km for global contours (prevents jagged lines at low zoom)
DOWNSAMPLED="${WORK_DIR}/gebco_1km.tif"
if [[ -f "$DOWNSAMPLED" ]]; then
    echo "1km downsampled raster exists, skipping."
else
    echo "Downsampling GEBCO to ~1km resolution (deep ocean contours)..."
    gdalwarp "$GEBCO_TIF" "$DOWNSAMPLED" \
        -tr 0.01 0.01 \
        -r cubicspline \
        -co COMPRESS=LZW \
        -co TILED=YES \
        -co BIGTIFF=YES \
        -overwrite
    echo "Done."
fi

# ~500m for shallow shelf contours (sharper detail in straits/fjords)
DOWNSAMPLED_SHELF="${WORK_DIR}/gebco_500m.tif"
if [[ -f "$DOWNSAMPLED_SHELF" ]]; then
    echo "500m downsampled raster exists, skipping."
else
    echo "Downsampling GEBCO to ~500m resolution (shelf contours)..."
    gdalwarp "$GEBCO_TIF" "$DOWNSAMPLED_SHELF" \
        -tr 0.005 0.005 \
        -r cubicspline \
        -co COMPRESS=LZW \
        -co TILED=YES \
        -co BIGTIFF=YES \
        -overwrite
    echo "Done."
fi

echo ""
echo "============================================="
echo "  Step 2: Extract Contours at Multiple Intervals"
echo "============================================="

# Major contours: every 1000m (visible z2+)
MAJOR="${WORK_DIR}/contours_major.geojson"
if [[ ! -f "$MAJOR" ]]; then
    echo "Extracting major contours (every 1000m)..."
    gdal_contour -a depth -i 1000 "$DOWNSAMPLED" "$MAJOR" -of GeoJSON
    echo "Done: $MAJOR"
else
    echo "Major contours exist, skipping."
fi

# Minor contours: every 500m (visible z3+)
MINOR="${WORK_DIR}/contours_500m.geojson"
if [[ ! -f "$MINOR" ]]; then
    echo "Extracting minor contours (every 500m)..."
    gdal_contour -a depth -i 500 "$DOWNSAMPLED" "$MINOR" -of GeoJSON
    echo "Done: $MINOR"
else
    echo "Minor contours exist, skipping."
fi

# Shelf contours: every 200m for depths 0 to -2000m (visible z4+)
SHELF="${WORK_DIR}/contours_200m.geojson"
if [[ ! -f "$SHELF" ]]; then
    echo "Extracting shelf contours (every 200m, 0 to -2000m)..."
    gdal_contour -a depth -i 200 -fl -100 -50 "$DOWNSAMPLED" "$SHELF" -of GeoJSON
    echo "Done: $SHELF"
else
    echo "Shelf contours exist, skipping."
fi

# Shallow contours: 10m, 20m, 50m (visible z6+) — key for Danish straits, North Sea shelf
# Uses finer 500m resolution raster for sharper detail
SHALLOW="${WORK_DIR}/contours_shallow.geojson"
if [[ ! -f "$SHALLOW" ]]; then
    echo "Extracting shallow contours (10m, 20m, 50m)..."
    gdal_contour -a depth -fl -150 -100 -75 -50 -40 -30 -20 -10 "$DOWNSAMPLED_SHELF" "$SHALLOW" -of GeoJSON
    echo "Done: $SHALLOW"
else
    echo "Shallow contours exist, skipping."
fi

echo ""
echo "============================================="
echo "  Step 3: Filter Ocean-Only & Add Properties"
echo "============================================="

# Process each contour set: filter ocean, add depth_class, simplify
process_contours() {
    local INPUT="$1"
    local OUTPUT="$2"
    local DEPTH_CLASS="$3"

    echo "Processing $DEPTH_CLASS contours..."
    # Remove existing output (GeoJSON driver can't overwrite)
    rm -f "$OUTPUT"
    # Filter ocean only (depth < 0) — NO simplification here, let tippecanoe handle it
    ogr2ogr "$OUTPUT" "$INPUT" \
        -where "depth < 0" \
        -f GeoJSON
}

MAJOR_OCEAN="${WORK_DIR}/contours_major_ocean.geojson"
MINOR_OCEAN="${WORK_DIR}/contours_500m_ocean.geojson"
SHELF_OCEAN="${WORK_DIR}/contours_200m_ocean.geojson"
SHALLOW_OCEAN="${WORK_DIR}/contours_shallow_ocean.geojson"

[[ ! -f "$MAJOR_OCEAN" ]] && process_contours "$MAJOR" "$MAJOR_OCEAN" "major"
[[ ! -f "$MINOR_OCEAN" ]] && process_contours "$MINOR" "$MINOR_OCEAN" "minor"
[[ ! -f "$SHELF_OCEAN" ]] && process_contours "$SHELF" "$SHELF_OCEAN" "shelf"
[[ ! -f "$SHALLOW_OCEAN" ]] && process_contours "$SHALLOW" "$SHALLOW_OCEAN" "shallow"

echo ""
echo "============================================="
echo "  Step 4: Generate PMTiles"
echo "============================================="

PMTILES="${OUTPUT_DIR}/depth_contours.pmtiles"
if [[ -f "$PMTILES" ]]; then
    echo "PMTiles already exists, skipping."
else
    echo "Generating depth contour PMTiles..."

    # Use zoom-dependent layer inputs so shallow contours don't compete
    # with major contours at low zoom levels:
    #   Major (1000m): z2-10
    #   Minor (500m): z3-10
    #   Shelf (200m): z4-10
    #   Shallow (10-150m): z6-10
    tippecanoe \
        -o "$PMTILES" \
        --minimum-zoom=2 \
        --maximum-zoom=10 \
        --simplification=2 \
        --no-feature-limit \
        --no-tile-size-limit \
        --force \
        -L "{\"file\":\"$MAJOR_OCEAN\",\"layer\":\"depth_contours\",\"minzoom\":2}" \
        -L "{\"file\":\"$MINOR_OCEAN\",\"layer\":\"depth_contours\",\"minzoom\":3}" \
        -L "{\"file\":\"$SHELF_OCEAN\",\"layer\":\"depth_contours\",\"minzoom\":4}" \
        -L "{\"file\":\"$SHALLOW_OCEAN\",\"layer\":\"depth_contours\",\"minzoom\":4}"

    echo "Done: $PMTILES"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "Major contours:  $MAJOR_OCEAN"
echo "Minor contours:  $MINOR_OCEAN"
echo "Shelf contours:  $SHELF_OCEAN"
echo "PMTiles:         $PMTILES"
if [[ -f "$PMTILES" ]]; then
    ls -lh "$PMTILES"
fi
echo ""
echo "Next: run 04-generate-ocean-labels.sh"
