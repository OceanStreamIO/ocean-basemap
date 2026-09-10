#!/usr/bin/env bash
# 04-generate-ocean-labels.sh — Natural Earth ocean labels → PMTiles
#
# Downloads Natural Earth marine polygons (oceans, seas, gulfs, bays, straits)
# and geography region points, extracts label centroids, assigns zoom levels,
# and generates PMTiles for the MapLibre basemap.
#
# Sources:
#   - ne_10m_geography_marine_polys (polygon → centroid labels)
#   - ne_10m_geography_regions_points (point labels for capes, ridges, etc.)
#
# Also attempts SCUFN Gazetteer (via NOAA ArcGIS REST) for undersea features
# (seamounts, trenches, fracture zones) — falls back gracefully if unavailable.

set -euo pipefail

BASEMAP_DIR="/media/andrei/RP60/oceanstream/basemap"
WORK_DIR="${BASEMAP_DIR}/gebco2025/work/labels"
OUTPUT_DIR="${BASEMAP_DIR}/gebco2025/tiles"

mkdir -p "$WORK_DIR"

NE_BASE="https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"

echo "============================================="
echo "  Step 1: Download Natural Earth Data"
echo "============================================="

# Marine polygons (oceans, seas, gulfs, bays, straits)
MARINE_POLYS="${WORK_DIR}/ne_10m_geography_marine_polys.geojson"
if [[ ! -f "$MARINE_POLYS" ]]; then
    echo "Downloading marine polygons..."
    curl -fSL -o "$MARINE_POLYS" "${NE_BASE}/ne_10m_geography_marine_polys.geojson"
    echo "Done."
else
    echo "Marine polygons exist, skipping."
fi

# Geography region points (capes, ridges, basins, etc.)
REGION_POINTS="${WORK_DIR}/ne_10m_geography_regions_points.geojson"
if [[ ! -f "$REGION_POINTS" ]]; then
    echo "Downloading geography region points..."
    curl -fSL -o "$REGION_POINTS" "${NE_BASE}/ne_10m_geography_regions_points.geojson"
    echo "Done."
else
    echo "Geography region points exist, skipping."
fi

echo ""
echo "============================================="
echo "  Step 2: Try SCUFN Gazetteer (optional)"
echo "============================================="

SCUFN_GEOJSON="${WORK_DIR}/scufn_features.geojson"
SCUFN_URL="https://gis.ngdc.noaa.gov/arcgis/rest/services/IHO/undersea_features/MapServer/0/query"
SCUFN_AVAILABLE=false

if [[ -f "$SCUFN_GEOJSON" ]]; then
    echo "SCUFN GeoJSON exists, using cached."
    SCUFN_AVAILABLE=true
else
    echo "Attempting SCUFN Gazetteer download (may be slow/unavailable)..."
    # Try paginated download from ArcGIS REST API (max 2000 per page)
    SCUFN_TEMP="${WORK_DIR}/scufn_page.json"
    OFFSET=0
    PAGE_SIZE=2000
    ALL_FEATURES=""
    SCUFN_OK=true

    # Test with a single record first (10s timeout)
    if curl -m 10 -fsSL "${SCUFN_URL}?where=1%3D1&outFields=Name,Generic&f=geojson&resultRecordCount=1" -o /dev/null 2>/dev/null; then
        echo "NOAA server reachable, downloading all features..."
        while true; do
            if ! curl -m 60 -fsSL "${SCUFN_URL}?where=1%3D1&outFields=Name,Generic&f=geojson&resultOffset=${OFFSET}&resultRecordCount=${PAGE_SIZE}" -o "$SCUFN_TEMP" 2>/dev/null; then
                echo "  Download failed at offset ${OFFSET}"
                SCUFN_OK=false
                break
            fi
            COUNT=$(python3 -c "import json; d=json.load(open('${SCUFN_TEMP}')); print(len(d.get('features',[])))" 2>/dev/null || echo "0")
            echo "  Page offset=${OFFSET}: ${COUNT} features"
            if [[ "$COUNT" == "0" ]]; then
                break
            fi
            OFFSET=$((OFFSET + PAGE_SIZE))
        done

        if [[ "$SCUFN_OK" == "true" && "$OFFSET" -gt 0 ]]; then
            # Merge all pages
            python3 << PYEOF
import json, glob, os
work = "${WORK_DIR}"
all_features = []
# Only one temp file at a time in this approach; re-download with merge
# Actually, let's just do a single big query
PYEOF
            # Simpler: single query with large limit
            if curl -m 120 -fsSL "${SCUFN_URL}?where=1%3D1&outFields=Name,Generic&f=geojson&resultRecordCount=10000" -o "$SCUFN_GEOJSON" 2>/dev/null; then
                FEAT_COUNT=$(python3 -c "import json; print(len(json.load(open('${SCUFN_GEOJSON}')).get('features',[])))" 2>/dev/null || echo "0")
                echo "SCUFN: ${FEAT_COUNT} features downloaded."
                if [[ "$FEAT_COUNT" -gt 100 ]]; then
                    SCUFN_AVAILABLE=true
                else
                    rm -f "$SCUFN_GEOJSON"
                fi
            fi
        fi
    else
        echo "NOAA server unreachable (timeout). Skipping SCUFN data."
    fi
    rm -f "$SCUFN_TEMP"
fi

echo ""
echo "============================================="
echo "  Step 3: Process & Merge Labels"
echo "============================================="

LABELED="${WORK_DIR}/ocean_labels_merged.geojson"
if [[ -f "$LABELED" ]]; then
    echo "Merged labels exist, skipping."
else
    echo "Processing labels..."
    python3 << PYEOF
import json
import math

output_features = []

# --- 1. Marine polygons → centroid point labels ---
print("Processing marine polygons...")
with open("${MARINE_POLYS}") as f:
    marine = json.load(f)

for feat in marine['features']:
    props = feat['properties']
    name = props.get('name', '')
    if not name:
        continue

    fclass = props.get('featurecla', '')
    ne_min_label = props.get('min_label', 5)
    scalerank = props.get('scalerank', 5)

    # Use Natural Earth's min_label as min_zoom (it's well-curated)
    min_zoom = max(1, int(round(ne_min_label)))

    # Determine text styling from feature class
    if fclass == 'ocean':
        text_transform = 'uppercase'
        text_size = 18
        label_type = 'ocean'
    elif fclass in ('sea', 'gulf'):
        text_transform = 'uppercase'
        text_size = 14
        label_type = 'sea'
    elif fclass in ('bay', 'strait', 'inlet'):
        text_transform = 'none'
        text_size = 12
        label_type = 'bay'
    else:
        text_transform = 'none'
        text_size = 11
        label_type = fclass or 'other'

    # Extract centroid from polygon geometry
    geom = feat['geometry']
    coords = geom.get('coordinates', [])

    def polygon_centroid(rings):
        """Compute centroid of outer ring."""
        if not rings or not rings[0]:
            return None, None
        ring = rings[0]
        n = len(ring)
        if n == 0:
            return None, None
        cx = sum(p[0] for p in ring) / n
        cy = sum(p[1] for p in ring) / n
        return cx, cy

    if geom['type'] == 'Polygon':
        cx, cy = polygon_centroid(coords)
    elif geom['type'] == 'MultiPolygon':
        # Use the largest polygon (most coords)
        largest = max(coords, key=lambda p: len(p[0]) if p else 0)
        cx, cy = polygon_centroid(largest)
    else:
        continue

    if cx is None:
        continue

    output_features.append({
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [round(cx, 4), round(cy, 4)]},
        'properties': {
            'name': name,
            'name_en': props.get('name_en', name),
            'label_type': label_type,
            'min_zoom': min_zoom,
            'text_transform': text_transform,
            'text_size': text_size,
            'scalerank': scalerank,
        }
    })

marine_count = len(output_features)
print(f"  {marine_count} marine labels from polygons")

# --- 2. Geography region points (marine-related only) ---
print("Processing region points...")
with open("${REGION_POINTS}") as f:
    regions = json.load(f)

MARINE_CLASSES = {'sea', 'ocean', 'gulf', 'bay', 'strait', 'inlet', 'sound',
                  'basin', 'ridge', 'trench', 'plateau', 'plain', 'rise',
                  'fracture zone', 'seamount', 'cap', 'cape', 'peninsula',
                  'island', 'island group', 'archipelago', 'reef', 'atoll'}

region_count = 0
for feat in regions['features']:
    props = feat['properties']
    fclass = (props.get('featurecla', '') or '').lower()
    name = props.get('name', '')
    if not name:
        continue

    # Include marine-related features
    min_zoom = props.get('min_zoom', 7)
    if isinstance(min_zoom, (int, float)):
        min_zoom = max(1, int(round(min_zoom)))
    else:
        min_zoom = 7

    output_features.append({
        'type': 'Feature',
        'geometry': feat['geometry'],
        'properties': {
            'name': name,
            'name_en': props.get('name_en', name),
            'label_type': fclass,
            'min_zoom': min_zoom,
            'text_transform': 'none',
            'text_size': 10,
            'scalerank': props.get('scalerank', 7),
        }
    })
    region_count += 1

print(f"  {region_count} region point labels")

# --- 3. SCUFN Gazetteer (if available) ---
scufn_count = 0
try:
    with open("${SCUFN_GEOJSON}") as f:
        scufn = json.load(f)

    SCUFN_ZOOM = {
        'Ocean': 1, 'Sea': 2, 'Gulf': 2, 'Bay': 3,
        'Basin': 3, 'Ridge': 3, 'Trench': 3, 'Trough': 3,
        'Plateau': 4, 'Rise': 4, 'Plain': 4,
        'Fracture zone': 5, 'Fracture Zone': 5, 'Escarpment': 5,
        'Seamount': 6, 'Seamounts': 6, 'Guyot': 6,
        'Bank': 6, 'Canyon': 6,
        'Sill': 7, 'Knoll': 7, 'Shoal': 7, 'Reef': 7,
        'Fan': 7, 'Channel': 7, 'Valley': 7,
        'Saddle': 8, 'Hill': 8, 'Pinnacle': 8,
    }

    # Build set of existing names to avoid duplicates
    existing_names = {f['properties']['name'].lower() for f in output_features}

    for feat in scufn.get('features', []):
        props = feat.get('properties', {})
        name = props.get('Name', props.get('name', ''))
        if not name or name.lower() in existing_names:
            continue

        generic = props.get('Generic', props.get('generic_term', 'Unknown'))
        zoom = 7
        for key, val in SCUFN_ZOOM.items():
            if key.lower() in (generic or '').lower():
                zoom = val
                break

        output_features.append({
            'type': 'Feature',
            'geometry': feat['geometry'],
            'properties': {
                'name': name.strip(),
                'name_en': name.strip(),
                'label_type': (generic or 'unknown').lower(),
                'min_zoom': zoom,
                'text_transform': 'uppercase' if zoom <= 3 else 'none',
                'text_size': 13 if zoom <= 3 else 11,
                'scalerank': zoom,
            }
        })
        existing_names.add(name.lower())
        scufn_count += 1
    print(f"  {scufn_count} SCUFN features added (de-duplicated)")
except FileNotFoundError:
    print("  SCUFN data not available, skipping.")

# Write merged output
geojson = {'type': 'FeatureCollection', 'features': output_features}
with open("${LABELED}", 'w') as f:
    json.dump(geojson, f)

total = len(output_features)
print(f"\nTotal: {total} labels ({marine_count} marine + {region_count} regions + {scufn_count} SCUFN)")

zoom_dist = {}
for feat in output_features:
    z = feat['properties']['min_zoom']
    zoom_dist[z] = zoom_dist.get(z, 0) + 1
for z in sorted(zoom_dist):
    print(f"  z{z}: {zoom_dist[z]} features")
PYEOF
fi

echo ""
echo "============================================="
echo "  Step 4: Generate PMTiles"
echo "============================================="

PMTILES="${OUTPUT_DIR}/ocean_labels.pmtiles"
if [[ -f "$PMTILES" ]]; then
    echo "PMTiles already exists, skipping."
else
    echo "Generating ocean label PMTiles..."
    tippecanoe \
        -o "$PMTILES" \
        -l ocean_labels \
        --minimum-zoom=1 \
        --maximum-zoom=10 \
        --no-feature-limit \
        --no-tile-size-limit \
        --force \
        "$LABELED"
    echo "Done: $PMTILES"
fi

echo ""
echo "============================================="
echo "  Summary"
echo "============================================="
echo "Marine polys:   $MARINE_POLYS"
echo "Region points:  $REGION_POINTS"
echo "Merged labels:  $LABELED"
echo "PMTiles:        $PMTILES"
if [[ -f "$PMTILES" ]]; then
    ls -lh "$PMTILES"
fi
echo ""
echo "Next: run 05-generate-land-tiles.sh"
