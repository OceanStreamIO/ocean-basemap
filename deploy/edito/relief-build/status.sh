#!/usr/bin/env bash
# =============================================================================
# status.sh — where the raster relief build Job has got to
# =============================================================================
#   ./deploy/edito/relief-build/status.sh          # one snapshot
#   watch -n 30 ./deploy/edito/relief-build/status.sh
#
# Read-only: job and pod state, CPU and memory, the step it is on, and how many
# tiles each archive has staged so far.
# =============================================================================
set -uo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
K=(kubectl -n "$NAMESPACE")
TILES=/home/onyxia/work/basemap-data/gebco2026/tiles

# What the Job publishes to, and so what to do when it finishes. Read from the
# Job itself rather than assumed, because a build with no version set writes
# straight to the live prefix and needs no switch at all.
version="$("${K[@]}" get job basemap-build \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="BASEMAP_XYZ_VERSION")].value}' 2>/dev/null)"
succeeded="$("${K[@]}" get job basemap-build -o jsonpath='{.status.succeeded}' 2>/dev/null)"

next_step() {
    if [[ -n "$version" ]]; then
        echo "Tree: basemap/$version/xyz  (NOT live; /xyz/ still serves the old one)"
        echo "  check:  https://basemap.oceanstream.io/$version/xyz/6/33/22.webp"
        echo "  go live: ./deploy/cdn/switch-xyz.py $version"
    else
        echo "Tree: basemap/xyz  (published live as it goes)"
    fi
}

pod="$("${K[@]}" get pod -l app=basemap-build -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [[ -z "$pod" ]]; then
    echo "No basemap-build pod. Job state:"
    "${K[@]}" get job basemap-build 2>&1
    # ttlSecondsAfterFinished removes the Job a day after it ends; the tiles in
    # S3 are permanent, the logs are not.
    [[ -n "$succeeded" ]] && { echo; next_step; }
    exit 0
fi

"${K[@]}" get job basemap-build -o jsonpath='Job: active={.status.active} succeeded={.status.succeeded} failed={.status.failed} started={.status.startTime}{"\n"}'
"${K[@]}" get pod "$pod" -o jsonpath='Pod: {.metadata.name} {.status.phase} on {.spec.nodeName}{"\n"}'
"${K[@]}" top pod "$pod" --no-headers 2>/dev/null \
    | awk '{printf "Use: %s CPU (limit 16), %s memory (limit 72Gi)\n", $2, $3}'

next_step
echo
echo "Step:"
"${K[@]}" logs "$pod" 2>/dev/null | tr '\r' '\n' \
    | grep -E '^(===|###)|Rendering|Writing|Done' | tail -3 | sed 's/^ */  /'

# Progress lives in the staging databases on the Job's volume. Staging is
# deleted once an archive is written, so finished archives are listed apart.
if [[ "$("${K[@]}" get pod "$pod" -o jsonpath='{.status.phase}')" == "Running" ]]; then
    echo "Progress:"
    "${K[@]}" exec "$pod" -- sh -c "
        cd /tmp/src/ocean-basemap 2>/dev/null && .venv/bin/python - <<'PY'
import glob, os, sqlite3
for f in sorted(glob.glob('$TILES/*.stage.mbtiles')):
    db = sqlite3.connect(f'file:{f}?mode=ro', uri=True)
    rows = db.execute('SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level').fetchall()
    done = db.execute('SELECT COUNT(*) FROM done').fetchone()[0]
    zooms = ', '.join(f'z{z} {n:,}' for z, n in rows)
    print(f'  {os.path.basename(f).removesuffix('.stage.mbtiles'):15s} {done:,} metatiles done   {zooms}')
for f in sorted(glob.glob('$TILES/*.pmtiles')):
    print(f'  {os.path.basename(f):28s} finished, {os.path.getsize(f) / 1e6:,.0f} MB')
PY" 2>/dev/null || echo "  (environment not installed yet)"
fi
