#!/usr/bin/env bash
# =============================================================================
# job.sh — measure both river networks, to compare their zoom rules
# =============================================================================
# Runs inside the river-lengths Job: fetches the code revision run.sh shipped,
# downloads the MERIT network and HydroRIVERS as drawn now, measures each
# (oceanbase river-lengths) and leaves the two small results beside the MERIT
# network. Fit from them anywhere with:
#     oceanbase river-compare merit.npz hydrorivers.npz
# =============================================================================
set -euo pipefail

: "${CODE_REV:?CODE_REV must name the code tarball to run}"
BUILD=s3://oceanstream-bucket/basemap-build
MERIT="$BUILD/out/rivers/merit_rivers.fgb"
HYDRO="$BUILD/inputs/gebco2026/work/land/hydrorivers.fgb"
OUT="$BUILD/out/rivers/lengths"
S3=(aws --endpoint-url "${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}" s3)
export HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools UV_TOOL_BIN_DIR=/tmp/uv-bin
export PATH="/tmp/uv-bin:$PATH" AWS_EC2_METADATA_DISABLED=true

echo "=== tools and code $CODE_REV ==="
uv tool install --quiet awscli
mkdir -p /tmp/src /tmp/data
"${S3[@]}" cp --only-show-errors "$BUILD/code/ocean-basemap-$CODE_REV.tar.gz" /tmp/src/code.tar.gz
tar -xzf /tmp/src/code.tar.gz -C /tmp/src
cd /tmp/src/ocean-basemap
uv sync --frozen --no-dev --quiet

echo "=== networks ==="
"${S3[@]}" cp --only-show-errors "$MERIT" /tmp/data/merit_rivers.fgb
"${S3[@]}" cp --only-show-errors "$HYDRO" /tmp/data/hydrorivers.fgb
ls -l /tmp/data

echo "=== measure $(date -Is) ==="
uv run --frozen --no-dev oceanbase river-lengths /tmp/data/hydrorivers.fgb /tmp/data/hydrorivers.npz &
hydro=$!
uv run --frozen --no-dev oceanbase river-lengths /tmp/data/merit_rivers.fgb /tmp/data/merit.npz
wait "$hydro"

echo "=== results → $OUT $(date -Is) ==="
"${S3[@]}" cp --only-show-errors /tmp/data/merit.npz "$OUT/merit.npz"
"${S3[@]}" cp --only-show-errors /tmp/data/hydrorivers.npz "$OUT/hydrorivers.npz"
uv run --frozen --no-dev oceanbase river-compare /tmp/data/merit.npz /tmp/data/hydrorivers.npz
