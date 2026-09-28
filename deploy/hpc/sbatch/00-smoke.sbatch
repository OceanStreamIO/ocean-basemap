#!/bin/bash
# =============================================================================
# Prove the environment on a compute node, cheaply
# =============================================================================
# A login node is not the thing that runs the build, and two of the pieces this
# depends on are only claimed to exist there: the interpreter the GDAL scripts
# are run by, and node-local scratch. This renders a thin slice of the tree and
# measures what it costs, so the sizing in the real templates is checked against
# a measurement rather than an estimate.
#
# Memory is reported as Pss, not RSS. Most of what a render worker touches is the
# mmapped archives -- shared, file-backed pages that every worker's RSS counts in
# full and the node holds one copy of. RSS overstates the cost by several times,
# which is how a worker count gets set too low.
#SBATCH --job-name=basemap-smoke
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-smoke-%j.out
#SBATCH --error=@LOGS@/basemap-smoke-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"

export BASEMAP_RENDER_WORKERS="@WORKERS@"
export GDAL_CACHEMAX="@GDAL_CACHEMAX@"

echo "=== smoke  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
echo "    cores=$SLURM_CPUS_PER_TASK workers=$BASEMAP_RENDER_WORKERS GDAL_CACHEMAX=${GDAL_CACHEMAX}M"
echo "    TMPDIR=$TMPDIR"

echo "### the things only a compute node can confirm"
printf '    %-28s %s\n' "node-local scratch" "$(df -Ph "$TMPDIR" | awk 'NR==2 {print $4" free on "$6}')"
for tool in gdalinfo gdalbuildvrt gdal_polygonize.py tippecanoe; do
    printf '    %-28s %s\n' "$tool" "$(command -v $tool || echo MISSING)"
done
printf '    %-28s %s\n' "gdal_polygonize.py runs" \
    "$(gdal_polygonize.py --help 2>&1 | head -1 | cut -c1-48)"
python3 -c "import rasterio, fiona, shapely, PIL, numpy
print(f'    {\"rasterio\":<28} {rasterio.__version__} (GDAL {rasterio.__gdal_version__})')
print(f'    {\"numpy / Pillow\":<28} {numpy.__version__} / {PIL.__version__}')"

echo "### how long does an unreachable host take to fail?"
# The offline contract assumes a refusal, not a hang. If this takes seconds the
# guard is belt and braces; if it takes minutes the guard is what makes the build
# possible at all.
python3 - <<'PY'
import time, httpx
start = time.time()
try:
    httpx.head("https://storage.googleapis.com/", timeout=20)
    print(f"    reachable in {time.time() - start:.1f}s -- unexpected on a compute node")
except Exception as exc:
    print(f"    failed in {time.time() - start:.1f}s: {type(exc).__name__}")
PY

echo "### mosaics the renderer refuses to start without  $(date -Is)"
oceanbase run surface-water

echo "### rendering 1/@SHARDS@ of the tree  $(date -Is)"
STAGE="$TMPDIR/smoke.mbtiles"
# Sampled in the background while the render runs: the peak is what matters and
# it does not happen at the end.
( while :; do
    total=0
    for p in /proc/*/smaps_rollup; do
        v=$(awk '/^Pss:/ {print $2}' "$p" 2>/dev/null || true)
        [ -n "${v:-}" ] && total=$((total + v))
    done
    echo "$total" >> "$TMPDIR/pss.log"
    sleep 20
  done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

/usr/bin/time -v oceanbase xyz-stage --shard 0 --of @SHARDS@ --stage "$STAGE" 2>&1 \
    | grep -E "Maximum resident|Elapsed|Percent of CPU|tiles|Staged|workers" || true

kill $SAMPLER 2>/dev/null || true
echo "### cost"
python3 - "$TMPDIR/pss.log" "$STAGE" "$BASEMAP_RENDER_WORKERS" <<'PY'
import pathlib, sqlite3, sys

log, stage, workers = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
if log.is_file():
    peaks = [int(v) for v in log.read_text().split() if v.isdigit()]
    if peaks:
        peak = max(peaks) / 1048576          # KiB -> GiB
        print(f"    peak proportional set size: {peak:.1f} GiB across the job")
        print(f"    per worker:                 {peak / workers:.2f} GiB")
        print(f"    112 workers would need:     {peak / workers * 112:.0f} GiB")
db = sqlite3.connect(f"file:{stage}?mode=ro", uri=True)
print(f"    tiles staged:               {db.execute('SELECT count(*) FROM tiles').fetchone()[0]:,}")
print(f"    bytes:                      {db.execute('SELECT sum(length(tile_data)) FROM tiles').fetchone()[0] or 0:,}")
PY
echo "=== done $(date -Is) ==="
