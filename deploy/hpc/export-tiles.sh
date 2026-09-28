#!/usr/bin/env bash
# =============================================================================
# Push the rendered shards and archives to object storage
# =============================================================================
# Gated on the sentinel the verify job writes, because this cannot be a
# scheduler dependency of the render: object storage is reachable only from a
# different Slurm cluster, so no job there can depend on a job here. Reading a
# sentinel is what makes this re-runnable and the driver re-entrant.
#
#   ./deploy/hpc/export-tiles.sh              # shards and archives
#   ./deploy/hpc/export-tiles.sh --only shards
#   ./deploy/hpc/export-tiles.sh --run 2026-09-28  # name the destination
#   ./deploy/hpc/export-tiles.sh --wait       # wait for the sentinel first
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

BUILD_PREFIX="${BASEMAP_BUILD_PREFIX:-basemap-build}"
RUN="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ONLY=""
WAIT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --only)    ONLY="${2:?--only needs shards or archives}"; shift ;;
        --run)     RUN="${2:?--run needs an identifier}"; shift ;;
        --wait)    WAIT=1 ;;
        -h|--help) sed -n '2,16p' "$0" >&2; exit 0 ;;
        *)         fail "Unknown argument: $1" ;;
    esac
    shift
done

require_site $HPC_SITE_VARS_XFER
require_hpc_key

SENTINEL="$HPC_STATE/xyz.done"
if [ "$WAIT" = "1" ]; then
    log "Waiting for $SENTINEL"
    waited=0
    while ! hpc_ssh_login "test -f '$SENTINEL'" 2>/dev/null; do
        [ "$waited" -ge "${HPC_POLL_TIMEOUT:-172800}" ] && fail "the sentinel never appeared"
        sleep "${HPC_POLL_INTERVAL:-60}"; waited=$((waited + ${HPC_POLL_INTERVAL:-60}))
        printf '%s    waiting %ss%s\r' "$_C_DIM" "$waited" "$_C_OFF" >&2
    done
    printf '\033[2K' >&2
fi

MANIFEST="$(hpc_ssh_login "cat '$SENTINEL' 2>/dev/null" 2>/dev/null || true)"
[ -n "$MANIFEST" ] || fail "No sentinel at $SENTINEL.

  The verify job writes it once the shards add up. Either the render has not
  finished (./deploy/hpc/status.sh) or it found the tree incomplete, in which
  case its log says which shard."

TILES="$(printf '%s' "$MANIFEST" | tr -d ' \n' | sed -n 's/.*"tiles":\([0-9]*\).*/\1/p')"
SHARDS="$(printf '%s' "$MANIFEST" | tr -d ' \n' | sed -n 's/.*"shards":\([0-9]*\).*/\1/p')"
log "Run $RUN: $SHARDS shards, ${TILES:-?} tiles"

FLAGS="--transfers=8 --checkers=16 --fast-list --retries=3 --stats=60s --stats-one-line"
DEST="$BUILD_PREFIX/xyz-shards/$RUN"

if [ -z "$ONLY" ] || [ "$ONLY" = "shards" ]; then
    log "Shards → $DEST"
    # shellcheck disable=SC2086
    hpc_rclone "shards" copy "$HPC_OUT/" "$(hpc_remote "$DEST/")" \
        --include='xyz.stage.*.mbtiles' $FLAGS \
        || fail "the shards did not transfer"

    # The manifest goes last, and the publisher refuses to start without it: its
    # presence is what says the set is complete and consistent.
    log "Manifest → $DEST/manifest.json"
    hpc_rclone "manifest" copy "$SENTINEL" "$(hpc_remote "$DEST/")" \
        || fail "the manifest did not transfer"
    hpc_rclone "manifest rename" moveto "$(hpc_remote "$DEST/xyz.done")" \
        "$(hpc_remote "$DEST/manifest.json")" \
        || warn "could not rename the manifest; the publisher expects manifest.json"

    # Verified by size rather than trusted: scratch filling up mid-write is the
    # failure this catches, and it is silent.
    log "Verifying"
    # shellcheck disable=SC2086
    hpc_rclone "shard check" check --size-only --one-way "$HPC_OUT/" \
        "$(hpc_remote "$DEST/")" --include='xyz.stage.*.mbtiles' --fast-list \
        || warn "the uploaded shards do not match scratch; re-run to resume"
fi

if [ -z "$ONLY" ] || [ "$ONLY" = "archives" ]; then
    log "Archives → $BUILD_PREFIX/out/tiles"
    # Into the build prefix, not the live one. Promoting an archive to the
    # published map is a separate, checked step -- deploy/edito/relief-build/promote.sh.
    # shellcheck disable=SC2086
    hpc_rclone "archives" copy "$HPC_DATA/gebco2026/tiles/" \
        "$(hpc_remote "$BUILD_PREFIX/out/tiles/")" --include='*.pmtiles' $FLAGS \
        || fail "the archives did not transfer"
fi

ok "Exported as run $RUN"
log "Publish it with:"
printf '    RUN_ID=%s ./deploy/edito/xyz-publish/run.sh\n' "$RUN" >&2
