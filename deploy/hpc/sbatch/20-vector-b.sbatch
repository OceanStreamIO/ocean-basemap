#!/bin/bash
# =============================================================================
# Vector archives built from the coastline, lakes and observed water.
# =============================================================================
# The order inside is forced, not arbitrary: sea-mask is the complement of the
# land that land-tiles builds, and places reads the geodatabase lakes unpacks.
#
# The memory driver here is the sea mask, which takes the complement of a 1.3 GB
# coastline; the cores are sized for that rather than for tippecanoe.
#SBATCH --job-name=basemap-vector-b
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --constraint=@CONSTRAINT@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=@CPUS@
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-vector-b-%j.out
#SBATCH --error=@LOGS@/basemap-vector-b-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"
# @EXPORTS@

echo "=== vector-b  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
for step in land-tiles sea-mask lakes surface-water places boundaries ocean-labels seamounts; do
    echo "### $step  $(date -Is)"
    oceanbase run "$step"
done
echo "=== done $(date -Is) ==="
