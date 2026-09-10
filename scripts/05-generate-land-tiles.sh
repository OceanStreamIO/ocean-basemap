#!/usr/bin/env bash
# 05-generate-land-tiles.sh — Natural Earth + OSM → PMTiles for land/coastline/cities
#
# Uses Natural Earth for low-zoom overview (z0-4) and OpenStreetMap land
# polygons + coastlines for high-zoom detail (z5-10).
# Layers: land polygons, coastlines, country boundaries, major cities.

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work/land"
OUTPUT_DIR="${BASEMAP_DIR}/gebco2025/tiles"

mkdir -p "$WORK_DIR"

echo "============================================="
echo "  Step 1a: Download Natural Earth Data"
echo "============================================="

NE_DIR="${WORK_DIR}/natural_earth"
mkdir -p "$NE_DIR"

download_ne() {
    local NAME="$1"
    local URL="$2"
    local ZIP="${NE_DIR}/${NAME}.zip"
    local SHP="${NE_DIR}/${NAME}.shp"

    if [[ -f "$SHP" ]]; then
        echo "  $NAME already downloaded."
        return
    fi

    echo "  Downloading $NAME..."
    curl -fSL -o "$ZIP" "$URL"
    cd "$NE_DIR"
    unzip -o "$ZIP" "${NAME}.*" 2>/dev/null || unzip -o "$ZIP"
    echo "  Done."
}

# 10m resolution (highest detail from NE)
download_ne "ne_10m_land" "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip"
download_ne "ne_10m_coastline" "https://naciscdn.org/naturalearth/10m/physical/ne_10m_coastline.zip"
download_ne "ne_10m_admin_0_countries" "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
download_ne "ne_10m_populated_places_simple" "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places_simple.zip"

# 50m (medium detail for lower zooms)
download_ne "ne_50m_land" "https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip"
download_ne "ne_50m_coastline" "https://naciscdn.org/naturalearth/50m/physical/ne_50m_coastline.zip"

# 110m (coarsest, for z0-2)
download_ne "ne_110m_land" "https://naciscdn.org/naturalearth/110m/physical/ne_110m_land.zip"

echo ""
echo "============================================="
echo "  Step 1b: Download OSM Coastline Data"
echo "============================================="

OSM_DIR="${WORK_DIR}/osm"
mkdir -p "$OSM_DIR"

# OSM land polygons — meter-level coastline precision
OSM_LAND_ZIP="${OSM_DIR}/land-polygons-split-4326.zip"
OSM_LAND_SHP="${OSM_DIR}/land-polygons-split-4326/land_polygons.shp"
if [[ -f "$OSM_LAND_SHP" ]]; then
    echo "  OSM land polygons already downloaded."
else
    echo "  Downloading OSM land polygons (~620MB)..."
    curl -fSL --progress-bar -o "$OSM_LAND_ZIP" \
        "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"
    echo "  Extracting..."
    cd "$OSM_DIR" && unzip -o "$OSM_LAND_ZIP"
    echo "  Done."
fi

# OSM coastlines (lines) — for the coastline stroke layer
OSM_COAST_ZIP="${OSM_DIR}/coastlines-split-4326.zip"
OSM_COAST_SHP="${OSM_DIR}/coastlines-split-4326/lines.shp"
if [[ -f "$OSM_COAST_SHP" ]]; then
    echo "  OSM coastlines already downloaded."
else
    echo "  Downloading OSM coastlines (~70MB)..."
    curl -fSL --progress-bar -o "$OSM_COAST_ZIP" \
        "https://osmdata.openstreetmap.de/download/coastlines-split-4326.zip"
    echo "  Extracting..."
    cd "$OSM_DIR" && unzip -o "$OSM_COAST_ZIP"
    echo "  Done."
fi

echo ""
echo "============================================="
echo "  Step 2: Convert Shapefiles to GeoJSON"
echo "============================================="

convert_to_geojson() {
    local SHP="$1"
    local OUT="$2"
    if [[ ! -f "$OUT" ]]; then
        echo "  Converting $(basename "$SHP") → GeoJSON..."
        ogr2ogr "$OUT" "$SHP" -f GeoJSON
    fi
}

# Natural Earth (for low-zoom)
convert_to_geojson "${NE_DIR}/ne_10m_land.shp" "${WORK_DIR}/land_10m.geojson"
convert_to_geojson "${NE_DIR}/ne_10m_coastline.shp" "${WORK_DIR}/coastline_10m.geojson"
convert_to_geojson "${NE_DIR}/ne_10m_admin_0_countries.shp" "${WORK_DIR}/countries_10m.geojson"
convert_to_geojson "${NE_DIR}/ne_10m_populated_places_simple.shp" "${WORK_DIR}/cities_10m.geojson"
convert_to_geojson "${NE_DIR}/ne_50m_land.shp" "${WORK_DIR}/land_50m.geojson"
convert_to_geojson "${NE_DIR}/ne_50m_coastline.shp" "${WORK_DIR}/coastline_50m.geojson"
convert_to_geojson "${NE_DIR}/ne_110m_land.shp" "${WORK_DIR}/land_110m.geojson"

# OSM (for high-zoom) — these are large, convert directly from SHP
# Note: ogr2ogr reads shapefiles natively, no need for an intermediate step
# We'll pass the SHP directly to tippecanoe via ogr2ogr pipe or FlatGeobuf

# Convert OSM to FlatGeobuf (faster for tippecanoe than GeoJSON for large files)
OSM_LAND_FGB="${WORK_DIR}/osm_land.fgb"
if [[ ! -f "$OSM_LAND_FGB" ]]; then
    echo "  Converting OSM land polygons → FlatGeobuf..."
    ogr2ogr "$OSM_LAND_FGB" "$OSM_LAND_SHP" -f FlatGeobuf
fi

OSM_COAST_FGB="${WORK_DIR}/osm_coastline.fgb"
if [[ ! -f "$OSM_COAST_FGB" ]]; then
    echo "  Converting OSM coastlines → FlatGeobuf..."
    ogr2ogr "$OSM_COAST_FGB" "$OSM_COAST_SHP" -f FlatGeobuf
fi

echo ""
echo "============================================="
echo "  Step 3: Generate PMTiles"
echo "============================================="

PMTILES="${OUTPUT_DIR}/land_base.pmtiles"
if [[ -f "$PMTILES" ]]; then
    echo "PMTiles already exists, skipping."
else
    echo "Generating land base PMTiles..."
    # Multi-resolution strategy:
    #   z0-2: NE 110m/50m (overview)
    #   z3-4: NE 10m (mid-detail)
    #   z5-10: OSM (meter-precision coastlines)
    #
    # --no-line-simplification keeps OSM coastlines pixel-perfect
    # --no-simplification-of-shared-nodes prevents gap artifacts
    tippecanoe \
        -o "$PMTILES" \
        --minimum-zoom=0 \
        --maximum-zoom=10 \
        --no-feature-limit \
        --no-tile-size-limit \
        --detect-shared-borders \
        --no-simplification-of-shared-nodes \
        --no-line-simplification \
        --force \
        -L '{"file":"'"${WORK_DIR}/land_110m.geojson"'","layer":"land","minzoom":0,"maxzoom":2}' \
        -L '{"file":"'"${WORK_DIR}/land_10m.geojson"'","layer":"land","minzoom":3,"maxzoom":4}' \
        -L '{"file":"'"${OSM_LAND_FGB}"'","layer":"land","minzoom":5}' \
        -L '{"file":"'"${WORK_DIR}/coastline_50m.geojson"'","layer":"coastline","minzoom":0,"maxzoom":2}' \
        -L '{"file":"'"${WORK_DIR}/coastline_10m.geojson"'","layer":"coastline","minzoom":3,"maxzoom":4}' \
        -L '{"file":"'"${OSM_COAST_FGB}"'","layer":"coastline","minzoom":5}' \
        -L '{"file":"'"${WORK_DIR}/countries_10m.geojson"'","layer":"countries","minzoom":2}' \
        -L '{"file":"'"${WORK_DIR}/cities_10m.geojson"'","layer":"cities","minzoom":2}'

    echo "Done: $PMTILES"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "PMTiles: $PMTILES"
if [[ -f "$PMTILES" ]]; then
    ls -lh "$PMTILES"
fi
echo ""
echo "Next: run 06-upload-azure.sh (when all tiles are ready)"
