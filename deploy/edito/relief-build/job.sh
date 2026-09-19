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
S3=(aws --endpoint-url "$BASEMAP_S3_ENDPOINT")

echo "=== Step 1: Tools ==="
uv tool install --quiet awscli
nproc_cg=$(awk '{print int($1/$2)}' /sys/fs/cgroup/cpu.max 2>/dev/null || echo "?")
echo "  cgroup CPUs: $nproc_cg · render workers: ${BASEMAP_RENDER_WORKERS:-8}"

echo "=== Step 2: Inputs ==="
mkdir -p "$DATA"
"${S3[@]}" s3 sync --only-show-errors "$BUILD/inputs/" "$DATA/"
du -sh "$DATA/gebco2026" | sed 's/^/  on volume: /'

echo "=== Step 3: Code $CODE_REV ==="
rm -rf /tmp/src && mkdir -p /tmp/src
"${S3[@]}" s3 cp --only-show-errors "$BUILD/code/ocean-basemap-$CODE_REV.tar.gz" /tmp/src/code.tar.gz
tar -xzf /tmp/src/code.tar.gz -C /tmp/src
cd /tmp/src/ocean-basemap
uv sync --frozen --no-dev --quiet
OB=(uv run --frozen --no-dev oceanbase)

echo "=== Step 4: Render ==="
for step in bathymetry hypsometric; do
    echo "### $step   $(date -Is)"
    "${OB[@]}" run "$step"
done

echo "=== Step 5: Upload to $BUILD/out/ ==="
BASEMAP_S3_PREFIX=basemap-build/out "${OB[@]}" run upload

echo "=== Done $(date -Is) ==="
