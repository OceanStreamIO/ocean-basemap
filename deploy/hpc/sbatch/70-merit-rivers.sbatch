#!/bin/bash
# =============================================================================
# The river network traced from MERIT Hydro (pipeline/merit_rivers.py).
# =============================================================================
# One whole node. The per-tile pass runs a worker per core over the ~1,100
# land tiles, each holding three 6000 x 6000 grids for a moment; the join then
# holds every river cell of the world at once -- about 225 bytes a cell,
# measured on a synthetic network, so roughly 60 GB at 10 km2. A standard
# node has room for both; the high-memory constraint is not needed.
#SBATCH --job-name=basemap-merit-rivers
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-merit-rivers-%j.out
#SBATCH --error=@LOGS@/basemap-merit-rivers-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"
# @EXPORTS@

export BASEMAP_RENDER_WORKERS="@WORKERS@"
export BASEMAP_MERIT_DIR="@MERIT@"
# Each worker reads its tiles once, start to finish; a block cache buys nothing.
export GDAL_CACHEMAX=64

echo "=== merit-rivers  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
echo "    workers=$BASEMAP_RENDER_WORKERS  packages=$BASEMAP_MERIT_DIR"
oceanbase run merit-rivers
echo "=== done $(date -Is) ==="
