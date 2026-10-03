#!/bin/bash
# =============================================================================
# The flat {z}/{x}/{y} tile tree, one array task per shard.
# =============================================================================
# Twenty-two million tiles, about a hundred core-hours. Each task renders every
# Nth column, so each samples the whole world and they cost the same; a
# contiguous block would be all open ocean or all coastline and the array would
# wait for the slowest.
#
# The staging database goes on node-local disk. It is opened write-ahead, and
# write-ahead logging needs a shared-memory mapping that a network filesystem
# does not provide; the finished file is copied to shared storage at the end.
#
# Unlike the raster render, this one forks its workers, and most of what they
# touch is the mmapped archives -- shared, so the whole node's cores fit.
#SBATCH --job-name=basemap-xyz
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --array=@ARRAY@
#SBATCH --output=@LOGS@/basemap-xyz-%A_%a.out
#SBATCH --error=@LOGS@/basemap-xyz-%A_%a.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"

SHARDS="@SHARDS@"
MAX_ZOOM="@MAX_ZOOM@"
SHARD="${SLURM_ARRAY_TASK_ID:-0}"
LOCAL="${TMPDIR:-/tmp}/xyz.stage.$(printf '%03d' "$SHARD")of$(printf '%03d' "$SHARDS").mbtiles"
FINAL="@OUT@/$(basename "$LOCAL")"

export BASEMAP_RENDER_WORKERS="@WORKERS@"
export GDAL_CACHEMAX="@GDAL_CACHEMAX@"
# Not just the --max-zoom argument: how far a tile may reach for a stand-in is
# derived from this, and that derivation has to reach the worker processes,
# which inherit the environment rather than the argument.
export BASEMAP_XYZ_MAX_ZOOM="$MAX_ZOOM"

echo "=== xyz shard $SHARD of $SHARDS to z$MAX_ZOOM  job ${SLURM_ARRAY_JOB_ID:-$SLURM_JOB_ID} on $(hostname) ==="
echo "    workers=$BASEMAP_RENDER_WORKERS  local stage=$LOCAL"
mkdir -p "@OUT@" "$(dirname "$LOCAL")"

# Resume onto whatever a previous attempt of this task left behind -- but only
# if the same renderer made it. A task is 20 minutes, so resuming matters less
# here than on the whole tree, and a requeued task should not start over.
#
# The fingerprint is what makes it safe. Without it, resume is indistinguishable
# from "the answer is already here": a palette change was made, the tree was
# re-rendered to pick it up, and every task copied the previous shard in, found
# all its columns present, skipped the lot and copied it back out. The counts
# all agreed, the tasks took their usual twenty minutes, and 358 million objects
# were published carrying the colour the change had replaced.
# Both trees, because the colours are not in the code: RAMP_DIR is the repo's
# own color-ramps/, so hashing src/basemap alone would call a tree rendered with
# a different palette "the same renderer" and resume it.
RENDERER="$(find "@ROOT@/code/src/basemap" "@ROOT@/code/color-ramps" \
                 -name '*.py' -o -name '*.txt' | sort \
            | xargs sha256sum | sha256sum | cut -c1-16)"
echo "    renderer $RENDERER"

if [ -f "$FINAL" ] && [ ! -f "$LOCAL" ]; then
    if [ "$(cat "$FINAL.renderer" 2>/dev/null)" = "$RENDERER" ]; then
        echo "    resuming from $FINAL"
        cp "$FINAL" "$LOCAL"
    else
        echo "    $FINAL was made by a different renderer; starting afresh"
        rm -f "$FINAL" "$FINAL.renderer"
    fi
fi

oceanbase xyz-stage --shard "$SHARD" --of "$SHARDS" --stage "$LOCAL" --max-zoom @MAX_ZOOM@

# The render asserts its own tile count before we get here, so reaching this
# line means the shard is complete. Copy, then verify the copy: shared storage
# filling up mid-write is the failure this catches.
echo "### copying to @OUT@  $(date -Is)"
cp "$LOCAL" "$FINAL.partial"
mv "$FINAL.partial" "$FINAL"
python3 - "$FINAL" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "copy is corrupt"
print(f"    {db.execute('SELECT count(*) FROM tiles').fetchone()[0]:,} tiles verified")
PY
# Stamped only once the copy has been verified, so a shard is never claimed by a
# renderer that did not finish writing it.
printf '%s\n' "$RENDERER" > "$FINAL.renderer"
rm -f "$LOCAL"
echo "=== done $(date -Is) ==="
