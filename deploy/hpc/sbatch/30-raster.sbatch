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

export BASEMAP_RENDER_WORKERS="@WORKERS@"
export GDAL_CACHEMAX="@GDAL_CACHEMAX@"

echo "=== raster  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
echo "    workers=$BASEMAP_RENDER_WORKERS  GDAL_CACHEMAX=${GDAL_CACHEMAX}M"
for step in hypsometric bathymetry globe; do
    echo "### $step  $(date -Is)"
    oceanbase run "$step"
done
echo "=== done $(date -Is) ==="
