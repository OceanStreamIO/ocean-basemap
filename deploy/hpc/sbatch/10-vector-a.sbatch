#!/bin/bash
# =============================================================================
# Vector archives built from the bathymetry grids. Rendered by submit.sh.
# =============================================================================
# Split from 20-vector-b only so the two can run at once: this group reads the
# global grid and the staged regional and high-resolution grids, that one reads
# the coastline, lakes and observed water. They share no inputs.
#
# No --mem. Some schedulers reject a memory request outright and derive RAM from
# the core count instead; where that is so, asking for cores IS asking for
# memory. Cores here are sized for tippecanoe, which is largely one process per
# archive, so this group does not get faster with a whole node.
#SBATCH --job-name=basemap-vector-a
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --constraint=@CONSTRAINT@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-vector-a-%j.out
#SBATCH --error=@LOGS@/basemap-vector-a-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"

echo "=== vector-a  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
for step in depth-polygons contours @OPTIONAL@; do
    echo "### $step  $(date -Is)"
    oceanbase run "$step"
done
echo "=== done $(date -Is) ==="
