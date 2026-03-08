#!/usr/bin/env bash
# 01-generate-hillshade.sh — Generate hillshade-only raster tiles from GEBCO
#
# This produces GRAYSCALE hillshade tiles (no color data).
# Color comes from vector depth polygons (script 02), not raster.
#
# Key quality improvements over previous attempt:
#   1. Multi-directional hillshade (-multidirectional) — illuminates from 4 directions
#   2. Higher z-factor (-z 8) — more vertical exaggeration for subtle ocean ridges
#   3. Lanczos resampling — smooth anti-aliased tile output (not nearest-neighbor)
#   4. Gamma correction — prevents washed-out midtones in the hillshade

set -euo pipefail

BASEMAP_DIR="/Volumes/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work"
TILE_DIR="${BASEMAP_DIR}/gebco2025/tiles/hillshade"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Use GEBCO 2025 if available, fall back to 2024
if [[ -f "${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif"
    echo "Using GEBCO 2025"
elif [[ -f "${BASEMAP_DIR}/work/gebco_2024.tif" ]]; then
    GEBCO_TIF="${BASEMAP_DIR}/work/gebco_2024.tif"
    echo "Using GEBCO 2024 (fallback)"
else
    echo "ERROR: No GEBCO GeoTIFF found."
    echo "  Expected: ${BASEMAP_DIR}/gebco2025/GEBCO_2025.tif"
    echo "  Run 00-download-gebco2025.sh first."
    exit 1
fi

mkdir -p "$WORK_DIR" "$TILE_DIR"

echo "============================================="
echo "  Step 1: Multi-directional Hillshade"
echo "============================================="

HILLSHADE="${WORK_DIR}/hillshade_multi.tif"
if [[ -f "$HILLSHADE" ]]; then
    echo "Hillshade already exists, skipping. Delete to regenerate."
else
    echo "Generating multi-directional hillshade (this takes ~20 min)..."
    gdaldem hillshade "$GEBCO_TIF" "$HILLSHADE" \
        -multidirectional \
        -z 8 \
        -compute_edges \
        -co COMPRESS=LZW \
        -co TILED=YES \
        -co BIGTIFF=YES \
        -co NUM_THREADS=ALL_CPUS
    echo "Done: $HILLSHADE"
fi

echo ""
echo "============================================="
echo "  Step 2: Gamma-Corrected Hillshade"
echo "============================================="

HILLSHADE_GAMMA="${WORK_DIR}/hillshade_gamma.tif"
if [[ -f "$HILLSHADE_GAMMA" ]]; then
    echo "Gamma-corrected hillshade already exists, skipping."
else
    echo "Applying gamma correction (γ=0.55) to prevent washed-out midtones..."
    # γ < 1 brightens midtones, making subtle relief more visible
    # without blowing out highlights or crushing shadows
    gdal_calc.py \
        -A "$HILLSHADE" \
        --outfile="$HILLSHADE_GAMMA" \
        --calc="255 * ((A / 255.0) ** 0.55)" \
        --type=Byte \
        --co=COMPRESS=LZW \
        --co=TILED=YES \
        --co=BIGTIFF=YES \
        --co=NUM_THREADS=ALL_CPUS
    echo "Done: $HILLSHADE_GAMMA"
fi

echo ""
echo "============================================="
echo "  Step 3: Generate XYZ Tile Tree (z0-10)"
echo "============================================="

if [[ -d "${TILE_DIR}/0" ]]; then
    echo "Tiles already exist at ${TILE_DIR}/, skipping."
    echo "Delete the directory to regenerate."
else
    echo "Generating tiles z0-10 with lanczos resampling (~4-5 hours)..."
    echo "Output: ${TILE_DIR}/"
    gdal2tiles.py "$HILLSHADE_GAMMA" "$TILE_DIR" \
        --zoom=0-10 \
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
echo "Hillshade:        $HILLSHADE"
echo "Gamma-corrected:  $HILLSHADE_GAMMA"
echo "Tiles:            ${TILE_DIR}/"
if [[ -d "${TILE_DIR}/0" ]]; then
    TILE_COUNT=$(find "$TILE_DIR" -name "*.png" | wc -l | tr -d ' ')
    echo "Tile count:       $TILE_COUNT"
fi
echo ""
echo "Next: run 02-generate-depth-polygons.sh"
