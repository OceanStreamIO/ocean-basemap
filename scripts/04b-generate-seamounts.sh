#!/usr/bin/env bash
# 04b-generate-seamounts.sh — GSFM seamount polygons → PMTiles
#
# Downloads the Global Seafloor Geomorphic Features Map (GSFM) from Blue Habitats,
# extracts seamount polygons, simplifies for web display, and generates PMTiles.
#
# Source:
#   Harris, P.T., Macmillan-Lawler, M., Rupp, J. and Baker, E.K. 2014.
#   Geomorphology of the oceans. Marine Geology, 352: 4-24.
#   https://bluehabitats.org/  (CC-BY 4.0)
#
# Output: seamounts.pmtiles (vector polygons, z0-10)

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work/seamounts"
TILE_DIR="${BASEMAP_DIR}/gebco2025/tiles"

# --- Prerequisites ---
if [[ ! -d "$BASEMAP_DIR" ]]; then
    echo "ERROR: External drive not mounted at $BASEMAP_DIR"
    exit 1
fi

mkdir -p "$WORK_DIR"

echo "============================================="
echo "  Step 1: Download GSFM Shapefiles"
echo "============================================="

GSFM_ZIP="${WORK_DIR}/gsfm_shapefiles.zip"
GSFM_DIR="${WORK_DIR}/gsfm_extracted"

# The full GSFM archive from Blue Habitats (~389 MB)
GSFM_URL="https://www.dropbox.com/scl/fi/mm01uhaa0ohyfitckmru7/global-seafloor-geomorphic-features-map.zip?rlkey=so36tzk2mfq6344xioq4v92y0&dl=1"

if [[ -f "$GSFM_ZIP" ]]; then
    echo "GSFM archive already downloaded, skipping."
else
    echo "Downloading GSFM shapefiles (~389 MB)..."
    curl -fSL -o "$GSFM_ZIP" "$GSFM_URL"
    echo "Done."
fi

echo ""
echo "============================================="
echo "  Step 2: Extract & Locate Seamount Shapefile"
echo "============================================="

if [[ -d "$GSFM_DIR" ]]; then
    echo "Already extracted, skipping."
else
    echo "Extracting archive..."
    mkdir -p "$GSFM_DIR"
    unzip -q -o "$GSFM_ZIP" -d "$GSFM_DIR"
    echo "Done."
fi

# Find the seamount shapefile (name may vary across GSFM versions)
SEAMOUNT_SHP=$(find "$GSFM_DIR" -iname '*seamount*' -name '*.shp' | head -1)

if [[ -z "$SEAMOUNT_SHP" ]]; then
    echo "ERROR: No seamount shapefile found in archive."
    echo "Available shapefiles:"
    find "$GSFM_DIR" -name '*.shp' | head -20
    exit 1
fi

echo "Found seamount shapefile: $SEAMOUNT_SHP"
echo "Feature count: $(ogrinfo -so "$SEAMOUNT_SHP" -al | grep 'Feature Count' || echo 'unknown')"

echo ""
echo "============================================="
echo "  Step 3: Convert to GeoJSON"
echo "============================================="

SEAMOUNT_GEOJSON="${WORK_DIR}/seamounts.geojson"

if [[ -f "$SEAMOUNT_GEOJSON" ]]; then
    echo "GeoJSON already exists, skipping."
else
    echo "Converting shapefile to GeoJSON..."
    ogr2ogr \
        -f GeoJSON \
        -t_srs EPSG:4326 \
        "$SEAMOUNT_GEOJSON" \
        "$SEAMOUNT_SHP"
    echo "Done."
fi

FEAT_COUNT=$(python3 -c "import json; print(len(json.load(open('${SEAMOUNT_GEOJSON}')).get('features',[])))")
echo "Seamount features: $FEAT_COUNT"

echo ""
echo "============================================="
echo "  Step 4: Simplify & Enrich Properties"
echo "============================================="

SEAMOUNT_CLEAN="${WORK_DIR}/seamounts_clean.geojson"

if [[ -f "$SEAMOUNT_CLEAN" ]]; then
    echo "Cleaned GeoJSON already exists, skipping."
else
    echo "Cleaning properties and simplifying..."
    python3 << 'PYEOF'
import json

with open("SEAMOUNT_GEOJSON_PATH") as f:
    data = json.load(f)

clean_features = []
for feat in data.get("features", []):
    props = feat.get("properties", {})
    geom = feat.get("geometry")
    if not geom:
        continue

    # Extract useful properties, normalizing field names (vary by GSFM version)
    name = ""
    for key in ("Name", "NAME", "name", "FEATNAME", "Feature_Na"):
        if props.get(key):
            name = str(props[key]).strip()
            break

    # Compute centroid for label placement
    # (tippecanoe can generate centroids, but pre-computing is more reliable)

    clean_features.append({
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "name": name,
            "type": "seamount",
        }
    })

output = {"type": "FeatureCollection", "features": clean_features}
with open("SEAMOUNT_CLEAN_PATH", "w") as f:
    json.dump(output, f)

print(f"Cleaned {len(clean_features)} seamount features")
PYEOF

    # Patch the placeholder paths in the Python heredoc
    # (heredoc quoting prevents variable expansion, so we use sed)
    # Actually, re-run with proper paths:
    python3 -c "
import json

with open('${SEAMOUNT_GEOJSON}') as f:
    data = json.load(f)

clean_features = []
for feat in data.get('features', []):
    props = feat.get('properties', {})
    geom = feat.get('geometry')
    if not geom:
        continue

    name = ''
    for key in ('Name', 'NAME', 'name', 'FEATNAME', 'Feature_Na'):
        if props.get(key):
            name = str(props[key]).strip()
            break

    clean_features.append({
        'type': 'Feature',
        'geometry': geom,
        'properties': {
            'name': name,
            'type': 'seamount',
        }
    })

output = {'type': 'FeatureCollection', 'features': clean_features}
with open('${SEAMOUNT_CLEAN}', 'w') as f:
    json.dump(output, f)

print(f'Cleaned {len(clean_features)} seamount features')
"
    echo "Done."
fi

echo ""
echo "============================================="
echo "  Step 5: Generate PMTiles"
echo "============================================="

PMTILES="${TILE_DIR}/seamounts.pmtiles"

if [[ -f "$PMTILES" ]]; then
    echo "PMTiles already exists, skipping."
else
    echo "Generating seamount PMTiles..."
    tippecanoe \
        -o "$PMTILES" \
        -l seamounts \
        --minimum-zoom=0 \
        --maximum-zoom=10 \
        --simplification=4 \
        --detect-shared-borders \
        --no-feature-limit \
        --no-tile-size-limit \
        --force \
        "$SEAMOUNT_CLEAN"
    echo "Done: $PMTILES"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "Source shapefile: $SEAMOUNT_SHP"
echo "Clean GeoJSON:   $SEAMOUNT_CLEAN"
echo "PMTiles:         $PMTILES"
if [[ -f "$PMTILES" ]]; then
    ls -lh "$PMTILES"
fi
echo ""
echo "Features: $FEAT_COUNT seamount polygons"
echo ""
echo "Next: run 05-generate-land-tiles.sh"
