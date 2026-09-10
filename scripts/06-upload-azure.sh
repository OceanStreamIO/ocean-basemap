#!/usr/bin/env bash
# 06-upload-azure.sh — Upload generated basemap tiles to Azure Blob Storage
#
# Uploads all generated assets to the ne1osvmdevtest storage account.
# Container: basemap/

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
TILE_DIR="${BASEMAP_DIR}/gebco2025/tiles"
STORAGE_ACCOUNT="ne1osvmdevtest"
CONTAINER="basemap"

# Check prerequisites
if ! command -v az &>/dev/null; then
    echo "ERROR: Azure CLI not found. Install with: brew install azure-cli"
    exit 1
fi

# Check Azure login
if ! az account show &>/dev/null; then
    echo "Not logged in to Azure. Running: az login"
    az login
fi

echo "============================================="
echo "  Uploading Basemap Assets to Azure"
echo "  Account: $STORAGE_ACCOUNT"
echo "  Container: $CONTAINER"
echo "============================================="

# Create container if it doesn't exist
az storage container create \
    --name "$CONTAINER" \
    --account-name "$STORAGE_ACCOUNT" \
    --public-access blob \
    2>/dev/null || true

echo ""
echo "--- Uploading hillshade raster tiles ---"
if [[ -d "${TILE_DIR}/hillshade/0" ]]; then
    az storage blob upload-batch \
        --destination "$CONTAINER" \
        --source "${TILE_DIR}/hillshade" \
        --destination-path "hillshade" \
        --account-name "$STORAGE_ACCOUNT" \
        --pattern "*.png" \
        --content-type "image/png" \
        --overwrite
    echo "Hillshade tiles uploaded."
else
    echo "SKIP: No hillshade tiles found."
fi

echo ""
echo "--- Uploading PMTiles ---"
for PMTILE_FILE in "${TILE_DIR}"/*.pmtiles; do
    if [[ -f "$PMTILE_FILE" ]]; then
        FILENAME=$(basename "$PMTILE_FILE")
        echo "Uploading $FILENAME..."
        az storage blob upload \
            --container-name "$CONTAINER" \
            --file "$PMTILE_FILE" \
            --name "tiles/${FILENAME}" \
            --account-name "$STORAGE_ACCOUNT" \
            --content-type "application/vnd.pmtiles" \
            --overwrite
    fi
done

echo ""
echo "============================================="
echo "  Upload Summary"
echo "============================================="
echo "Blob base URL: https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/"
echo ""
echo "Assets:"
echo "  Hillshade:      https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/hillshade/{z}/{x}/{y}.png"
echo "  Depth zones:    https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/tiles/depth_zones.pmtiles"
echo "  Depth contours: https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/tiles/depth_contours.pmtiles"
echo "  Ocean labels:   https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/tiles/ocean_labels.pmtiles"
echo "  Land base:      https://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/tiles/land_base.pmtiles"
