#!/usr/bin/env bash
# =============================================================================
# The gate: render a thin slice on a compute node and measure what it cost
# =============================================================================
# Everything the templates assume is checked here, on the machine that will
# actually run the build rather than on the login node:
#
#   * the interpreter the GDAL scripts are run by exists on a compute node
#   * node-local scratch exists and has room
#   * the virtual environment and the wrappers resolve
#   * an unreachable host fails quickly rather than hanging
#   * what a render worker really costs, measured as Pss rather than RSS
#
# Uses the short, high-priority queue, so it starts in minutes rather than
# tomorrow.
#
#   ./deploy/hpc/smoke-test.sh                 # 1/4096th of the tree
#   ./deploy/hpc/smoke-test.sh --shards 512    # a bigger slice
#   ./deploy/hpc/smoke-test.sh --render-only
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

SHARDS="${SMOKE_SHARDS:-4096}"
CPUS="${SMOKE_CPUS:-16}"
WORKERS="${SMOKE_WORKERS:-16}"
WALLTIME="${SMOKE_TIME:-01:00:00}"
CACHEMAX="${SMOKE_CACHEMAX:-64}"
RENDER_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --shards)      SHARDS="${2:?--shards needs a number}"; shift ;;
        --render-only) RENDER_ONLY=1 ;;
        -h|--help)     sed -n '2,20p' "$0" >&2; exit 0 ;;
        *)             fail "Unknown argument: $1" ;;
    esac
    shift
done

# Modest and short, so the debug queue takes it. A sixteenth of a node measures
# the per-worker cost just as well as a whole one, and that is what is wanted.
RENDERED="$(sed -e "s|@ACCOUNT@|${HPC_ACCOUNT:-}|g" \
                -e "s|@QOS@|${HPC_DEBUG_QOS:-${HPC_QOS:-}}|g" \
                -e "s|@PARTITION@|${HPC_PARTITION:-}|g" \
                -e "s|@ROOT@|$HPC_ROOT|g" \
                -e "s|@LOGS@|$HPC_LOGS|g" \
                -e "s|@CPUS@|$CPUS|g" \
                -e "s|@WORKERS@|$WORKERS|g" \
                -e "s|@WALLTIME@|$WALLTIME|g" \
                -e "s|@GDAL_CACHEMAX@|$CACHEMAX|g" \
                -e "s|@SHARDS@|$SHARDS|g" \
                "$SBATCH_DIR/00-smoke.sbatch")"

if [ "$RENDER_ONLY" = "1" ]; then
    printf '%s\n' "$RENDERED"
    exit 0
fi

require_site $HPC_SITE_VARS_SLURM
require_hpc_key
hpc_ssh_login "test -f '$HPC_ROOT/etc/basemap-env.sh'" 2>/dev/null \
    || fail "The cluster environment is not set up. Run ./deploy/hpc/bootstrap.sh first."

log "1/$SHARDS of the tree on $CPUS cores, $WORKERS workers, qos ${HPC_DEBUG_QOS:-$HPC_QOS}"
printf '%s\n' "$RENDERED" | hpc_test_only basemap-smoke || fail "the scheduler would not take it"
printf '%s\n' "$RENDERED" | hpc_submit_wait login basemap-smoke
