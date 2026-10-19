#!/bin/bash
# =============================================================================
# The raster archives: land relief, continuous bathymetry, and the globe grid.
# =============================================================================
# This is the one group that wants a high-memory node. Its renderer spawns its
# workers rather than forking them, and a worker holding a dozen open source
# grids levels off around 3.5 GiB, none of it shared -- so on a node with two
# gigabytes per core the worker count is capped by memory at well under the core
# count, and a high-memory node runs the whole node's worth.
#
# GDAL's block cache defaults to a share of total RAM, which on a large node is
# gigabytes per process. Pinned, or ninety-odd workers will not fit.
#SBATCH --job-name=basemap-raster
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --constraint=@CONSTRAINT@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-raster-%j.out
#SBATCH --error=@LOGS@/basemap-raster-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"
# @EXPORTS@

export BASEMAP_RENDER_WORKERS="@WORKERS@"
export GDAL_CACHEMAX="@GDAL_CACHEMAX@"

echo "=== raster  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
echo "    workers=$BASEMAP_RENDER_WORKERS  GDAL_CACHEMAX=${GDAL_CACHEMAX}M"

# The EMODnet mosaic, which the regional and HR bathymetry read. It is not
# staged -- a .vrt names its sources by path -- and without it both archives
# are built from the global grid alone and nothing says so. Its own step would
# rebuild it, but only on the way to the depth-zone vectors, an hour of
# polygonising that nothing in this job reads. So it is built here from the
# staged chunks, in the order that step lists them, which decides which chunk
# wins where two overlap.
python3 - <<'PY'
from basemap.config import get_data_dir, get_work_dir
from basemap.pipeline.emodnet import chunk_bboxes, chunk_name
from basemap.utils.gdal import gdalbuildvrt

work = get_work_dir(get_data_dir()) / "emodnet"
vrt = work / "emodnet_mean.vrt"
if vrt.is_file():
    print(f"    {vrt.name} present")
else:
    chunks = [work / "chunks" / chunk_name(box) for box in chunk_bboxes()]
    present = [c for c in chunks if c.is_file() and c.stat().st_size > 0]
    if not present:
        raise SystemExit(f"no EMODnet chunks under {work / 'chunks'}; stage the inputs first")
    gdalbuildvrt(present, vrt)
    print(f"    {vrt.name} built from {len(present):,} of {len(chunks):,} chunks")
PY
# RASTER_STEPS narrows the stage to the archives whose inputs actually
# changed. A land-ramp change does not touch the bathymetry, and that is
# the expensive one.
for step in @RASTER_STEPS@; do
    echo "### $step  $(date -Is)"
    oceanbase run "$step"
done
echo "=== done $(date -Is) ==="
