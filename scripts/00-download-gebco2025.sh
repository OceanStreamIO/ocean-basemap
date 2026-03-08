#!/usr/bin/env bash
# 00-download-gebco2025.sh — Download GEBCO 2025 GeoTIFF from CEDA and merge
#
# Downloads 8 GeoTIFF tiles (90°x90° each, ~890 MB each, ~7.1 GB total)
# directly from CEDA (no account required), then merges into a single file.
#
# Source: https://data.ceda.ac.uk/bodc/gebco/global/gebco_2025/ice_surface_elevation/geotiff
# License: Public domain
# Attribution: GEBCO Compilation Group (2025) GEBCO 2025 Grid (doi:10.5285/37c52e96-24ea-67ce-e063-7086abc05f29)

set -euo pipefail

BASEMAP_DIR="/Volumes/RP60/oceanstream/basemap"
GEBCO_DIR="${BASEMAP_DIR}/gebco2025"

CEDA_BASE="https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2025/ice_surface_elevation/geotiff"

# 8 tiles covering the globe (90°x90° each)
TILES=(
    "gebco_2025_n90.0_s0.0_w-180.0_e-90.0.tif"
    "gebco_2025_n90.0_s0.0_w-90.0_e0.0.tif"
    "gebco_2025_n90.0_s0.0_w0.0_e90.0.tif"
    "gebco_2025_n90.0_s0.0_w90.0_e180.0.tif"
    "gebco_2025_n0.0_s-90.0_w-180.0_e-90.0.tif"
    "gebco_2025_n0.0_s-90.0_w-90.0_e0.0.tif"
    "gebco_2025_n0.0_s-90.0_w0.0_e90.0.tif"
    "gebco_2025_n0.0_s-90.0_w90.0_e180.0.tif"
)

# MD5 checksums from CEDA (bash 3.2 compatible — no associative arrays)
MD5_EXPECTED=(
    "5339d30e2a4f4ae766a436d33ab185af"
    "add2bd85c08106e47d67a1802ae8355a"
    "e93523f35c40d65e14e15724960cbe2d"
    "5ef662c016a17d6eaf03841f840d629c"
    "4fc4ce0052dc91463a9ab43521cbc7d8"
    "39b88b46c36e999021d6bf38e21a794c"
    "cc7820f063dc547c8711c367b9dbb06c"
    "da7235029d598c53c509e56e7472239d"
)

# Check external drive is mounted
if [[ ! -d "$BASEMAP_DIR" ]]; then
    echo "ERROR: External drive not mounted at /Volumes/RP60"
    echo "  Mount the drive and try again."
    exit 1
fi

mkdir -p "$GEBCO_DIR"

echo "============================================="
echo "  GEBCO 2025 Grid — Download from CEDA"
echo "============================================="
echo ""

# Check if already merged
if [[ -f "${GEBCO_DIR}/GEBCO_2025.tif" ]]; then
    echo "GEBCO 2025 GeoTIFF already exists at ${GEBCO_DIR}/GEBCO_2025.tif"
    gdalinfo "${GEBCO_DIR}/GEBCO_2025.tif" 2>/dev/null | head -10
    exit 0
fi

# Download tiles
echo "Downloading 8 GeoTIFF tiles (~890 MB each, ~7.1 GB total)..."
echo ""

DOWNLOADED=0
SKIPPED=0

for i in "${!TILES[@]}"; do
    tile="${TILES[$i]}"
    EXPECTED="${MD5_EXPECTED[$i]}"
    DEST="${GEBCO_DIR}/${tile}"
    URL="${CEDA_BASE}/${tile}?download=1"

    if [[ -f "$DEST" ]]; then
        # Verify existing file with MD5
        ACTUAL=$(md5 -q "$DEST" 2>/dev/null || md5sum "$DEST" | cut -d' ' -f1)
        if [[ "$ACTUAL" == "$EXPECTED" ]]; then
            echo "  ✓ ${tile} (already downloaded, MD5 verified)"
            SKIPPED=$((SKIPPED + 1))
            continue
        else
            echo "  ✗ ${tile} exists but MD5 mismatch — re-downloading"
            rm "$DEST"
        fi
    fi

    echo "  ↓ Downloading ${tile}..."
    curl -L -C - --progress-bar -o "$DEST" "$URL"

    # Verify MD5
    ACTUAL=$(md5 -q "$DEST" 2>/dev/null || md5sum "$DEST" | cut -d' ' -f1)
    if [[ "$ACTUAL" != "$EXPECTED" ]]; then
        echo "  ✗ MD5 MISMATCH for ${tile}!"
        echo "    Expected: ${EXPECTED}"
        echo "    Got:      ${ACTUAL}"
        echo "    Deleting corrupt file. Re-run the script to retry."
        rm "$DEST"
        exit 1
    fi
    echo "  ✓ ${tile} (MD5 verified)"
    DOWNLOADED=$((DOWNLOADED + 1))
done

echo ""
echo "Download complete: ${DOWNLOADED} new, ${SKIPPED} already had."
echo ""

# Merge all 8 tiles into single GeoTIFF
echo "Merging 8 tiles into GEBCO_2025.tif..."
echo "  (This creates an ~8 GB file with LZW compression)"
echo ""

gdal_merge.py \
    -o "${GEBCO_DIR}/GEBCO_2025.tif" \
    "${GEBCO_DIR}"/gebco_2025_*.tif \
    -co COMPRESS=LZW \
    -co TILED=YES \
    -co BIGTIFF=YES

echo ""
echo "Done! GEBCO 2025 GeoTIFF ready."
echo ""
gdalinfo "${GEBCO_DIR}/GEBCO_2025.tif" | head -20
