#!/bin/bash
# =============================================================================
# The observed-water archive the viewer draws (pipeline/coastal_water.py).
# =============================================================================
# One whole node. Global Surface Water is read tile by tile from z8 to z12,
# only under tiles that held water at the level above. Since the archive took
# in inland water -- every river bed and pond, not just the coast -- that is
# millions of tiles rather than tens of thousands, which is why it runs here.
#SBATCH --job-name=basemap-observed-water
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-observed-water-%j.out
#SBATCH --error=@LOGS@/basemap-observed-water-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"
# @EXPORTS@

export BASEMAP_RENDER_WORKERS="@WORKERS@"
# Each worker warps its own strips; a large block cache per process would only
# multiply the memory by the worker count.
export GDAL_CACHEMAX=128

echo "=== observed-water  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
echo "    workers=$BASEMAP_RENDER_WORKERS"
oceanbase run coastal-water
echo "=== done $(date -Is) ==="
