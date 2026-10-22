#!/usr/bin/env bash
# =============================================================================
# Trace the river network from MERIT Hydro on the cluster
# =============================================================================
#   ./deploy/hpc/rivers.sh               # stage, submit and wait, export
#   ./deploy/hpc/rivers.sh stage         # one phase: stage, submit or export
#   ./deploy/hpc/rivers.sh submit --test-only
#
# The MERIT packages (dir_*.tar, upa_*.tar, wth_*.tar) are downloaded on the
# datalab side into object storage first -- the cluster may not connect out --
# and staged from there to scratch, where the job reads them in place, with
# the runoff grid the discharge is estimated from (oceanbase merit-runoff). The
# same job runs the rivers step on the network. Three things come back: the
# network to basemap-build/out/rivers/, the staged rivers the flat tree reads
# to the build inputs, and the vector archive to basemap-build/out/tiles/, from
# where promote.sh publishes it. See docs/pipeline/merit-rivers.md.
#
# Re-running is safe: staging copies, a queued job of the same name is adopted
# rather than submitted twice, and the tile pass resumes per tile.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

JOB_NAME=basemap-merit-rivers
MERIT_PREFIX="${BASEMAP_MERIT_PREFIX:-basemap-build/merit_hydro}"
OUT_PREFIX="${BASEMAP_RIVERS_PREFIX:-basemap-build/out/rivers}"
MERIT_DIR="$HPC_STAGE/merit_hydro"
NETWORK="$HPC_DATA/gebco2026/work/land/merit_rivers.fgb"
STAGED="$HPC_DATA/gebco2026/work/land/rivers.fgb"
ARCHIVE="$HPC_DATA/gebco2026/tiles/rivers.pmtiles"
INPUTS_PREFIX="${BASEMAP_INPUTS_PREFIX:-basemap-build/inputs/gebco2026}"
TILES_PREFIX="${BASEMAP_TILES_PREFIX:-basemap-build/out/tiles}"
CPUS="${RIVERS_CPUS:-112}"
WORKERS="${RIVERS_WORKERS:-$CPUS}"
WALLTIME="${RIVERS_TIME:-03:00:00}"

PHASES="${1:-all}"
shift || true
TEST_ONLY=0
[ "${1:-}" = "--test-only" ] && TEST_ONLY=1
[ "$PHASES" = "all" ] && PHASES="stage submit export"
case "$PHASES" in -h|--help) sed -n '2,17p' "$0" >&2; exit 0 ;; esac

require_site
require_hpc_key

stage() {
    log "MERIT Hydro packages and the runoff grid → $MERIT_DIR"
    hpc_xfer pull --src "$(hpc_s3 "$MERIT_PREFIX/")" --dest "$MERIT_DIR" \
        --include '*.tar' --include 'grun_*.tif' \
        || fail "the MERIT packages did not transfer"
}

# The same placeholders submit.sh fills, for the ones this template has. The
# threshold is the one setting worth carrying over from here.
render() {
    local exports=""
    if [ -n "${BASEMAP_MERIT_MIN_UPA:-}" ]; then
        case "$BASEMAP_MERIT_MIN_UPA" in *[!0-9.]*) fail "BASEMAP_MERIT_MIN_UPA is a number of km2" ;; esac
        exports="export BASEMAP_MERIT_MIN_UPA='$BASEMAP_MERIT_MIN_UPA'\\n"
    fi
    sed -e "s|@ACCOUNT@|${HPC_ACCOUNT}|g" -e "s|@QOS@|${HPC_QOS}|g" \
        -e "s|@PARTITION@|${HPC_PARTITION}|g" -e "s|@ROOT@|${HPC_ROOT}|g" \
        -e "s|@LOGS@|${HPC_LOGS}|g" -e "s|@CPUS@|${CPUS}|g" -e "s|@WORKERS@|${WORKERS}|g" \
        -e "s|@WALLTIME@|${WALLTIME}|g" -e "s|@MERIT@|${MERIT_DIR}|g" \
        "$SBATCH_DIR/70-merit-rivers.sbatch" \
    | awk -v exports="$exports" '$0 == "# @EXPORTS@" { printf "%s", exports; next } { print }'
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

export_network() {
    log "River network → $OUT_PREFIX/merit_rivers.fgb"
    hpc_xfer push --file --always --src "$NETWORK" --dest "$(hpc_s3 "$OUT_PREFIX/merit_rivers.fgb")" \
        || fail "the network did not transfer"
    log "Staged rivers → $INPUTS_PREFIX/work/land/rivers.fgb"
    hpc_xfer push --file --always --src "$STAGED" \
        --dest "$(hpc_s3 "$INPUTS_PREFIX/work/land/rivers.fgb")" \
        || fail "the staged rivers did not transfer"
    log "Vector archive → $TILES_PREFIX/rivers.pmtiles"
    hpc_xfer push --file --always --src "$ARCHIVE" --dest "$(hpc_s3 "$TILES_PREFIX/rivers.pmtiles")" \
        || fail "the rivers archive did not transfer"
}

for phase in $PHASES; do
    case "$phase" in
        stage)  stage ;;
        submit) submit ;;
        export) export_network ;;
        *)      fail "unknown phase: $phase (stage, submit, export or all)" ;;
    esac
done
ok "rivers: $PHASES done"
