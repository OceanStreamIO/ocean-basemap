#!/usr/bin/env bash
# =============================================================================
# Build the observed-water archive on the cluster
# =============================================================================
#   ./deploy/hpc/water.sh                # submit and wait, export
#   ./deploy/hpc/water.sh submit         # one phase: submit or export
#   ./deploy/hpc/water.sh submit --test-only
#
# coastal_water.pmtiles is the observed water the viewer draws: Global Surface
# Water, inland as well as on the coast. It reads what the render chain has
# already staged -- the occurrence mosaics and the GEBCO grid -- so it stages
# nothing of its own. The archive goes to basemap-build/out/tiles/, from where
# promote.sh publishes it. See docs/hpc.md, "Observed water".
#
# Re-running is safe: a queued job of the same name is adopted rather than
# submitted twice. The step itself starts its archive over each time.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

JOB_NAME=basemap-observed-water
TILES_PREFIX="${BASEMAP_TILES_PREFIX:-basemap-build/out/tiles}"
ARCHIVE="$HPC_DATA/gebco2026/tiles/coastal_water.pmtiles"
CPUS="${WATER_CPUS:-112}"
WORKERS="${WATER_WORKERS:-$CPUS}"
WALLTIME="${WATER_TIME:-24:00:00}"

PHASES="${1:-all}"
shift || true
TEST_ONLY=0
[ "${1:-}" = "--test-only" ] && TEST_ONLY=1
[ "$PHASES" = "all" ] && PHASES="submit export"
case "$PHASES" in -h|--help) sed -n '2,15p' "$0" >&2; exit 0 ;; esac

require_site
require_hpc_key

render() {
    sed -e "s|@ACCOUNT@|${HPC_ACCOUNT}|g" -e "s|@QOS@|${HPC_QOS}|g" \
        -e "s|@PARTITION@|${HPC_PARTITION}|g" -e "s|@ROOT@|${HPC_ROOT}|g" \
        -e "s|@LOGS@|${HPC_LOGS}|g" -e "s|@CPUS@|${CPUS}|g" -e "s|@WORKERS@|${WORKERS}|g" \
        -e "s|@WALLTIME@|${WALLTIME}|g" \
        "$SBATCH_DIR/60-observed-water.sbatch" \
    | awk '$0 == "# @EXPORTS@" { next } { print }'
}

submit() {
    require_site $HPC_SITE_VARS_SLURM
    if [ "$TEST_ONLY" = "1" ]; then
        render | hpc_test_only "$JOB_NAME" >/dev/null || fail "the scheduler refused the job"
        ok "The scheduler would accept $JOB_NAME"
        return 0
    fi
    local found
    found="$(hpc_ssh_login "squeue -u '$HPC_USER' -h -n '$JOB_NAME' -o '%i' 2>/dev/null | head -1" 2>/dev/null \
             | tr -d '[:space:]' | grep -E '^[0-9]+' || true)"
    if [ -n "$found" ]; then
        warn "$JOB_NAME is already queued as $found; waiting on it rather than submitting again"
        hpc_poll "$found" login "$JOB_NAME"
        return
    fi
    render | hpc_submit_wait login "$JOB_NAME"
}

export_archive() {
    log "Observed water → $TILES_PREFIX/coastal_water.pmtiles"
    hpc_xfer push --file --always --src "$ARCHIVE" --dest "$(hpc_s3 "$TILES_PREFIX/coastal_water.pmtiles")" \
        || fail "the archive did not transfer"
}

for phase in $PHASES; do
    case "$phase" in
        submit) submit ;;
        export) export_archive ;;
        *)      fail "unknown phase: $phase (submit, export or all)" ;;
    esac
done
ok "water: $PHASES done"
