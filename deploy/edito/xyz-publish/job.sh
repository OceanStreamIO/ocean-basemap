#!/usr/bin/env bash
# =============================================================================
# job.sh — the flat-tile publish, inside the basemap-xyz-publish Job
# =============================================================================
# Reads the shards the cluster rendered out of object storage and sends the
# tiles in them, one object per tile.
#
# A shard at a time on local disk: the set is about 31 GB and the pod has no
# room for all of it, so each is fetched, published, and deleted. The resume
# marks are on a volume that outlives the pod, so an interruption costs the
# column in flight rather than the run.
# =============================================================================
set -euo pipefail

: "${CODE_REV:?CODE_REV must name the code tarball to run}"
: "${RUN_ID:?RUN_ID must name the render to publish}"

BUILD=s3://oceanstream-bucket/basemap-build
SHARD_PREFIX="$BUILD/xyz-shards/$RUN_ID"
WORK=/work
MARKS=/marks
INDEX="${JOB_COMPLETION_INDEX:-0}"
PODS="${PUBLISH_PODS:-1}"

export HOME=/tmp
export UV_CACHE_DIR="$WORK/.uv-cache"
export UV_TOOL_DIR=/tmp/uv-tools UV_TOOL_BIN_DIR=/tmp/uv-bin
export PATH="/tmp/uv-bin:$PATH"
S3=(aws --endpoint-url "$BASEMAP_S3_ENDPOINT")

echo "=== Step 1: tools ==="
# Stagger the start by index, then retry. Eight pods come up at once and the
# first thing each does is resolve pypi.org; measured, that is enough to make
# DNS answer "Try again" for several of them, and a pod that cannot install its
# tools dies before it has done any work -- burning a retry, leaving a corpse
# against the namespace's pod quota, and taking the Job's backoff limit with it.
# Neither the stagger nor the retry is clever; both are cheap next to a shard.
sleep "$(( (INDEX % 8) * 15 ))"
for attempt in 1 2 3 4 5 6; do
    uv tool install --quiet awscli && break
    echo "  tool install attempt $attempt failed; retrying" >&2
    sleep $((attempt * 20))
done
command -v aws >/dev/null || { echo "  could not install the aws CLI" >&2; exit 1; }
echo "  pod $INDEX of $PODS · run $RUN_ID · version ${BASEMAP_XYZ_VERSION:-<live>}"
echo "  $(nproc) cores visible, ${BASEMAP_XYZ_PUBLISH_PROCESSES} processes × ${BASEMAP_XYZ_PUBLISH_WORKERS} threads"

echo "=== Step 2: code $CODE_REV ==="
rm -rf /tmp/src && mkdir -p /tmp/src
"${S3[@]}" s3 cp --only-show-errors "$BUILD/code/ocean-basemap-$CODE_REV.tar.gz" /tmp/src/code.tar.gz
tar -xzf /tmp/src/code.tar.gz -C /tmp/src
cd /tmp/src/ocean-basemap
uv sync --frozen --no-dev --quiet
OB=(uv run --frozen --no-dev oceanbase)

echo "=== Step 3: manifest ==="
# Written last by the export, so its presence means the shard set is complete
# and consistent. Refusing to start without it is the difference between
# publishing a whole map and publishing most of one.
"${S3[@]}" s3 cp --only-show-errors "$SHARD_PREFIX/manifest.json" "$WORK/manifest.json" \
    || { echo "No manifest at $SHARD_PREFIX/manifest.json." >&2
         echo "Run deploy/hpc/export-tiles.sh for this run first." >&2; exit 1; }

read -r SHARDS EXPECTED < <(python3 - "$WORK/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(m["shards"], m["tiles"])
PY
)
echo "  $SHARDS shards, $EXPECTED tiles expected"

# Which shards this pod owns. With one pod that is all of them; the arithmetic
# is here so that raising completions is the only change needed.
MINE=()
for ((i = INDEX; i < SHARDS; i += PODS)); do MINE+=("$i"); done
echo "  this pod publishes shards: ${MINE[*]}"

echo "=== Step 4: publish ==="
TOTAL=0
for shard in "${MINE[@]}"; do
    name="$(printf 'xyz.stage.%03dof%03d.mbtiles' "$shard" "$SHARDS")"
    marker="$SHARD_PREFIX/$name.published"
    if "${S3[@]}" s3 ls "$marker" >/dev/null 2>&1; then
        echo "### shard $shard already published, skipping"
        continue
    fi

    echo "### shard $shard: fetching $name  $(date -Is)"
    "${S3[@]}" s3 cp --only-show-errors "$SHARD_PREFIX/$name" "$WORK/$name"

    # What the manifest says this shard holds. The count was asserted when it was
    # rendered and again when the set was verified, but neither of those saw it
    # come back out of object storage -- and a shard that arrives short would
    # publish a hole rather than fail.
    want="$(python3 - "$WORK/manifest.json" "$shard" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(next(f["tiles"] for f in m["files"] if f["shard"] == int(sys.argv[2])))
PY
)"

    echo "### shard $shard: sending $want tiles  $(date -Is)"
    # The marks live on the volume, not beside the shard, because the shard is
    # deleted after each one and the marks must not be.
    "${OB[@]}" xyz-publish --stage "$WORK/$name" --marks "$MARKS/xyz.sent.sqlite" \
        --expect "$want"

    # The marker goes up only after the send returned successfully, so a failed
    # shard is retried rather than skipped.
    printf 'run=%s shard=%s finished=%s\n' "$RUN_ID" "$shard" "$(date -Is)" \
        | "${S3[@]}" s3 cp --only-show-errors - "$marker"
    rm -f "$WORK/$name"
    TOTAL=$((TOTAL + 1))
    echo "### shard $shard done  $(date -Is)"
done

echo "=== Step 5: check ==="
# Every shard this pod owns must carry its marker, and the marker is written
# only after that shard's send returned. That is the whole statement a pod can
# honestly make: the tree is complete when all of the markers exist, which is
# the publisher's caller's job to check, not this pod's.
#
# This used to count rows in the marks database against sum(1 << z for z in
# range(13)). Three things were wrong with that and all of them bit. The count
# was the column total for a z12 tree, so a deeper one could never satisfy it.
# The marks are per pod and hold only that pod's shards, so it was comparing a
# stride against the whole tree. And a pod whose shards were all published by an
# earlier run has no marks database at all, so it died on "unable to open
# database file" -- which is what stopped this Job finishing: index 3 had
# nothing left to do, could not succeed, and took the run down with it.
missing=0
for shard in "${MINE[@]}"; do
    name="$(printf 'xyz.stage.%03dof%03d.mbtiles' "$shard" "$SHARDS")"
    if "${S3[@]}" s3 ls "$SHARD_PREFIX/$name.published" >/dev/null 2>&1; then
        echo "  shard $shard: published"
    else
        echo "  shard $shard: NO MARKER" >&2
        missing=$((missing + 1))
    fi
done
[ "$missing" -eq 0 ] || { echo "  $missing of this pod's shards did not finish" >&2; exit 1; }

echo "=== Done $(date -Is): $TOTAL shard(s) published by pod $INDEX ==="
if [ -n "${BASEMAP_XYZ_VERSION:-}" ]; then
    echo
    echo "The tree is at basemap/$BASEMAP_XYZ_VERSION/xyz and is NOT live."
    echo "Check it, then point the CDN at it:"
    echo "    ./deploy/cdn/switch-xyz.py --dry-run $BASEMAP_XYZ_VERSION"
    echo "    ./deploy/cdn/switch-xyz.py $BASEMAP_XYZ_VERSION"
fi
