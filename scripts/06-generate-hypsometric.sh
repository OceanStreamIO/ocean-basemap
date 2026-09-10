#!/usr/bin/env bash
# 06-generate-hypsometric.sh — Generate hypsometric color-relief land tiles from GEBCO
#
# Creates EMODnet-style land coloring: green lowlands → tan hills → brown mountains → white peaks.
# Hillshade blending is done in MapLibre (not GDAL) for flexibility.
#
# Pipeline:
#   1. Create land-only DEM (ocean → nodata)
#   2. Generate color-relief with -alpha (nodata → transparent)
#   3. Tile with gdal2tiles (z0-10)
#
# Prerequisites:
#   - GEBCO 2025 GeoTIFF
#   - GDAL tools (gdaldem, gdal_calc.py, gdal2tiles.py)
#
# Output:
#   - /media/andrei/RP60/oceanstream/basemap/gebco2025/tiles/hypsometric/ (XYZ PNG tiles z0-10)

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work"
TILE_DIR="${BASEMAP_DIR}/gebco2025/tiles/hypsometric"

if [[ -f "${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif"
elif [[ -f "${BASEMAP_DIR}/work/gebco_2024.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/work/gebco_2024.tif"
else
    echo "ERROR: No GEBCO GeoTIFF found."
    exit 1
fi

mkdir -p "$WORK_DIR/hypsometric" "$TILE_DIR"

echo "============================================="
echo "  Step 1: Create Hypsometric Color Ramp"
echo "============================================="

COLOR_RAMP="${WORK_DIR}/hypsometric/hypsometric_ramp.txt"
cat > "$COLOR_RAMP" << 'EOF'
# EMODnet-style hypsometric color ramp
# Format: elevation  R  G  B
# Coastal lowlands → green → tan → brown → gray → white
-1      170 210 220
0       196 218 158
10      200 220 160
25      205 222 150
50      210 215 145
100     215 215 140
150     218 210 135
200     220 205 130
300     222 200 125
500     218 190 120
750     210 175 115
1000    200 160 110
1500    190 145 105
2000    180 140 105
2500    175 150 135
3000    185 170 150
4000    200 195 185
5000    218 215 210
6000    235 232 228
7000    245 243 240
8000    252 250 248
9000    255 254 252
nv     0  0  0  0
EOF
echo "Color ramp written: $COLOR_RAMP"

echo ""
echo "============================================="
echo "  Step 2: Create Land-Only DEM (ocean → nodata)"
echo "============================================="

# Set ocean pixels (elevation < 0) to nodata=-9999.
# gdaldem color-relief with -alpha will make nodata → transparent.
LAND_DEM="${WORK_DIR}/hypsometric/gebco_land_only.tif"
if [[ -f "$LAND_DEM" ]]; then
    echo "Land-only DEM already exists, skipping."
else
    echo "Masking ocean to nodata (elevation < 0 → -9999)..."
    echo "This processes the full GEBCO raster (~20 min)..."
    gdal_calc.py \
        -A "$GEBCO_TIF" \
        --outfile="$LAND_DEM" \
        --calc="numpy.where(A >= 0, A, -9999)" \
        --NoDataValue=-9999 \
        --type=Float32 \
        --co=COMPRESS=LZW \
        --co=TILED=YES \
        --co=BIGTIFF=YES \
        --co=NUM_THREADS=ALL_CPUS
    echo "Done: $LAND_DEM"
fi

echo ""
echo "============================================="
echo "  Step 3: Generate Color-Relief (ocean = transparent)"
echo "============================================="

# gdaldem color-relief with -alpha produces RGBA output.
# Nodata pixels (-9999) get alpha=0 (fully transparent) automatically.
COLOR_RELIEF="${WORK_DIR}/hypsometric/hypsometric_land.tif"
if [[ -f "$COLOR_RELIEF" ]]; then
    echo "Color-relief already exists, skipping."
else
    echo "Generating hypsometric color-relief (~15 min)..."
    gdaldem color-relief "$LAND_DEM" "$COLOR_RAMP" "$COLOR_RELIEF" \
        -alpha \
        -co COMPRESS=LZW \
        -co TILED=YES \
        -co BIGTIFF=YES \
        -co NUM_THREADS=ALL_CPUS
    echo "Done: $COLOR_RELIEF"
fi

echo ""
echo "============================================="
echo "  Step 4: Generate XYZ Tile Tree (z0-10)"
echo "============================================="

if [[ -d "${TILE_DIR}/0" ]]; then
    echo "Tiles already exist at ${TILE_DIR}/, skipping."
    echo "Delete the directory to regenerate."
else
    echo "Generating hypsometric tiles z0-8 (~1-2 hours)..."
    echo "Output: ${TILE_DIR}/"
    gdal2tiles.py "$COLOR_RELIEF" "$TILE_DIR" \
        --zoom=0-8 \
        --processes=8 \
        --xyz \
        --resampling=lanczos \
        --tilesize=256 \
        --no-kml \
        --webviewer=none
    echo "Done! Tile tree at ${TILE_DIR}/"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "Color ramp:       $COLOR_RAMP"
echo "Land-only DEM:    $LAND_DEM"
echo "Color relief:     $COLOR_RELIEF"
echo "Tiles:            ${TILE_DIR}/"
if [[ -d "${TILE_DIR}/0" ]]; then
    TILE_COUNT=$(find "$TILE_DIR" -name "*.png" | wc -l | tr -d ' ')
    echo "Tile count:       $TILE_COUNT"
fi
echo ""
echo "Hillshade blending is done in MapLibre (hillshade-land layer on top)."
echo "No GDAL hillshade blending step needed — keeps the pipeline simple."
