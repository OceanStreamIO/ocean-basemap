#!/usr/bin/env bash
# =============================================================================
# job.sh — the raster relief build, run inside the basemap-build Job
# =============================================================================
# Every step is idempotent: `s3 sync` skips what the volume already holds, the
# DEM download skips files on disk, and the renderer resumes from its staging
# database. Rerunning the Job after an interruption picks up where it stopped.
#
# The volume is mounted at the path it has on the Jupyter pod, so the mosaics
# built there (absolute paths to thousands of DEM tiles) resolve unchanged.
#
# Finished archives go to the private basemap-build/out/ prefix, not to the
# public basemap/ one: promoting them is a separate, checked step.
# =============================================================================
set -euo pipefail

: "${CODE_REV:?CODE_REV must name the code tarball to run}"
DATA=/home/onyxia/work/basemap-data
BUILD=s3://oceanstream-bucket/basemap-build
export HOME=/tmp
export BASEMAP_DATA_DIR="$DATA"
export UV_CACHE_DIR="$DATA/.uv-cache"          # on the volume: wheels survive reruns
export UV_TOOL_DIR=/tmp/uv-tools UV_TOOL_BIN_DIR=/tmp/uv-bin
export PATH="/tmp/uv-bin:$PATH"
export AWS_EC2_METADATA_DISABLED=true          # not on EC2; the probe otherwise stalls
# The flat tree goes to a version, not the live prefix. Publishing it takes
# about as long as rendering it, so writing in place left the public map half
# old and half new for most of a day. With XYZ_VERSION unset it publishes
# live, exactly as before.
S3=(aws --endpoint-url "$BASEMAP_S3_ENDPOINT")

echo "=== Step 1: Tools ==="
uv tool install --quiet awscli
nproc_cg=$(awk '{print int($1/$2)}' /sys/fs/cgroup/cpu.max 2>/dev/null || echo "?")
echo "  cgroup CPUs: $nproc_cg · render workers: ${BASEMAP_RENDER_WORKERS:-8}"

echo "=== Step 2: Inputs ==="
# Everything under basemap-build/inputs/ lands at the same path in $DATA, so
# adding a source to the render means staging it there first. What the steps
# below read, beyond the GEBCO grid:
#
#   work/emodnet, work/emodnet_hr   the regional and HR bathymetry
#   work/land_dem                   Copernicus DEM, for the land relief
#   work/land/osm_land.fgb          the coastline every mask is cut from
#   work/land/rivers_10m.geojson    river centrelines, drawn into the tree
#   work/lakes/lakes_{1000,100,10,1,all}.fgb   the lake bands, by zoom
#   work/water/gsw/                 observed surface water, for the sea mask
#   tiles/depth_contours.pmtiles    the contours the flat tree bakes in
#
# The last four are read by the xyz step and the bathymetry. Missing lakes or
# contours is a hard failure rather than a silent tree without them -- see
# pipeline/xyz.py -- so a rebuild started without them stops in the first
# seconds. The contour archive was not staged for the first full build, and
# twenty-two million tiles came out with no contours in any of them.
mkdir -p "$DATA"
"${S3[@]}" s3 sync --only-show-errors "$BUILD/inputs/" "$DATA/"
du -sh "$DATA/gebco2026" | sed 's/^/  on volume: /'

# Archives named in REBUILD are cleared, with their staging, so the render step
# builds them afresh instead of skipping them as done.
#
# Once per Job, not once per attempt. A Job that is retried -- a node going
# NotReady, an eviction -- runs this script again from the top, and clearing
# on every attempt threw away twelve hours of finished archives because the
# first attempt died in a later step. The marker records what this Job already
# cleared, so a retry resumes onto the work instead of destroying it.
#
# Keyed on the Job, not the code revision: two runs at the same revision are a
# normal thing to want -- three candidate palettes are exactly that -- and the
# revision-keyed marker made the second and third silently skip their clear.
CLEARED="$DATA/gebco2026/tiles/.cleared-${JOB_UID:-$CODE_REV}"
if [[ -n "${REBUILD:-}" && ! -f "$CLEARED" ]]; then
    for name in $REBUILD; do
        rm -fv "$DATA/gebco2026/tiles/$name.pmtiles" "$DATA/gebco2026/tiles/$name.stage.mbtiles"
    done
    mkdir -p "$(dirname "$CLEARED")"
    echo "$REBUILD" > "$CLEARED"
elif [[ -n "${REBUILD:-}" ]]; then
    echo "  REBUILD already applied for $CODE_REV; keeping what is built."
fi

echo "=== Step 3: Code $CODE_REV ==="
rm -rf /tmp/src && mkdir -p /tmp/src
"${S3[@]}" s3 cp --only-show-errors "$BUILD/code/ocean-basemap-$CODE_REV.tar.gz" /tmp/src/code.tar.gz
tar -xzf /tmp/src/code.tar.gz -C /tmp/src
cd /tmp/src/ocean-basemap
uv sync --frozen --no-dev --quiet
OB=(uv run --frozen --no-dev oceanbase)
# Fail in seconds, with the reason, if the image lacks a library the wheels link.
uv run --frozen --no-dev python -c \
    "import rasterio, fiona, shapely, PIL.Image; print('  GDAL', rasterio.__gdal_version__)"

echo "=== Step 4: Render ==="
# The globe step too: land_geo and bathy_geo are the same specs on the
# geographic grid, so a ramp change reaches them and they go stale silently --
# the poles kept the old sea colour for two days after the Mercator archives
# were rebuilt, because nothing here built them.
# xyz last: it composites the finished raster archives into a flat tile tree
# and publishes it, so every archive it reads has to exist first.
# BASEMAP_XYZ_REDO_FROM is passed straight through: the xyz step reads it to
# clear resume marks at and below a zoom, for when the reason a tile was
# missing has been fixed and "already done" is no longer the truth.
# BUILD_STEPS exists so one archive can be rebuilt on its own -- a new step, or
# one whose inputs changed -- without re-rendering the three that take a day.
for step in ${BUILD_STEPS:-bathymetry hypsometric globe xyz}; do
    echo "### $step   $(date -Is)"
    "${OB[@]}" run "$step"
done

echo "=== Step 5: Upload to $BUILD/out/ ==="
BASEMAP_S3_PREFIX=basemap-build/out "${OB[@]}" run upload

echo "=== Done $(date -Is) ==="
if [[ -n "${BASEMAP_XYZ_VERSION:-}" ]]; then
    echo "The flat tree is at basemap/$BASEMAP_XYZ_VERSION/xyz and is NOT live."
    echo "Check it, then: ./deploy/cdn/switch-xyz.py $BASEMAP_XYZ_VERSION"
fi
