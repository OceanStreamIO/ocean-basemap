#!/usr/bin/env bash
# =============================================================================
# Free the staging filesystem, once the bytes are safely elsewhere
# =============================================================================
# The allocation's scratch quota is shared with other projects, so a build that
# leaves 200 GB behind is a build that stops someone else's.
#
# Nothing is deleted without first confirming the object store holds it. A
# verification that was not actually performed -- because the check job was
# detached, or errored -- counts as a failure, not a pass.
#
#   ./deploy/hpc/cleanup-staging.sh --shards    # after publishing
#   ./deploy/hpc/cleanup-staging.sh --inputs    # after the whole build
#   ./deploy/hpc/cleanup-staging.sh --merit     # once the river network is traced
#   ./deploy/hpc/cleanup-staging.sh --dry-run --all
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

BUILD_PREFIX="${BASEMAP_BUILD_PREFIX:-basemap-build}"
DRY_RUN=0; DO_SHARDS=0; DO_INPUTS=0; DO_MERIT=0; RUN="${RUN_ID:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --shards)  DO_SHARDS=1 ;;
        --inputs)  DO_INPUTS=1 ;;
        --merit)   DO_MERIT=1 ;;
        --all)     DO_SHARDS=1; DO_INPUTS=1 ;;
        --run)     RUN="${2:?--run needs an identifier}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,16p' "$0" >&2; exit 0 ;;
        *)         fail "Unknown argument: $1" ;;
    esac
    shift
done
[ "$DO_SHARDS$DO_INPUTS$DO_MERIT" = "000" ] && fail "Nothing to do: pass --shards, --inputs, --merit or --all"

require_site $HPC_SITE_VARS_XFER
require_hpc_key

# A path that is not one of ours is not ours to delete. The allocation is
# shared, and a mistyped variable must not turn into rm -rf on someone else's
# products.
assert_ours() {
    case "$1" in
        */oceanstream/basemap/*) ;;
        *) fail "refusing to delete outside the basemap subtree: $1" ;;
    esac
    [ "${#1}" -gt 20 ] || fail "suspiciously short path, refusing: $1"
}

remove() {
    local path="$1"
    assert_ours "$path"
    if [ "$DRY_RUN" = "1" ]; then
        hpc_ssh_login "du -sh '$path' 2>/dev/null | sed 's|^|    would remove |'" 2>/dev/null || true
        return 0
    fi
    hpc_ssh_login "rm -rf '$path' && echo '    removed $path'" 2>/dev/null || warn "could not remove $path"
}

if [ "$DO_SHARDS" = "1" ]; then
    [ -n "$RUN" ] || fail "--shards needs --run <id>, to know which upload to verify against"
    log "Confirming the object store holds run $RUN before deleting anything"
    if hpc_xfer check --src "$HPC_OUT" --dest "$(hpc_s3 "$BUILD_PREFIX/xyz-shards/$RUN/")" \
            --include 'xyz.stage.*.mbtiles'; then
        ok "the shards are in the object store"
        remove "$HPC_OUT"
    else
        fail "the object store does not match scratch -- keeping the shards.
  Re-run ./deploy/hpc/export-tiles.sh --run $RUN first."
    fi
fi

# The MERIT Hydro packages (56 GB) and the runoff grid are copies of
# basemap-build/merit_hydro/, staged for tracing the river network; the
# per-tile cells are only a cache that lets a re-trace skip the extraction.
if [ "$DO_MERIT" = "1" ]; then
    log "Confirming the object store holds the MERIT packages before deleting anything"
    if hpc_xfer check --src "$HPC_STAGE/merit_hydro" --dest "$(hpc_s3 "$BUILD_PREFIX/merit_hydro/")" \
            --include '*.tar' --include 'grun_*.tif'; then
        ok "the packages are in the object store"
        remove "$HPC_STAGE/merit_hydro"
        remove "$HPC_DATA/gebco2026/work/merit_rivers"
    else
        fail "the object store does not match scratch -- keeping the packages."
    fi
fi

if [ "$DO_INPUTS" = "1" ]; then
    warn "The inputs are ~200 GB and take about an hour to stage again."
    confirm "Remove the staged inputs?"
    remove "$HPC_DATA"
fi

hpc_ssh_login "df -Pk '$HPC_STAGE' 2>/dev/null | awk 'NR==2 {printf \"    %.0f GB free on scratch\n\", \$4/1048576}'" 2>/dev/null || true
ok "Done"
