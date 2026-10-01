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

# head -1, because every shard carries its own "tiles" too and `.*` is greedy:
# the old pattern reported 22,369,616 -- one shard's count -- for a tree of
# 357,913,941, and the publisher then expected a sixteenth of the work.
field() { printf '%s' "$MANIFEST" | tr -d ' \n' | grep -o "\"$1\":[0-9]*" | head -1 | cut -d: -f2; }
TILES="$(field tiles)"
SHARDS="$(field shards)"
DEPTH="$(field max_zoom)"
log "Run $RUN: $SHARDS shards, ${TILES:-?} tiles, z0-${DEPTH:-?}"

# The sentinel is a file on a shared filesystem with no run in its name, so one
# left by an earlier render looks exactly like one written for this one. That
# happened: a z13 sentinel from the previous night was still there when a z14
# render was submitted, --wait returned immediately, and the export began
# shipping a directory that was still being written, under the old run's
# manifest. Checking the depth is what turns that into a refusal.
if [ -n "${XYZ_MAX_ZOOM:-}" ] && [ -n "$DEPTH" ] && [ "$DEPTH" != "$XYZ_MAX_ZOOM" ]; then
    fail "The sentinel describes a z0-$DEPTH render and this run asked for z0-$XYZ_MAX_ZOOM.
  It is left over from an earlier build. Delete it once the render in flight has
  finished and the verify job will write the right one:
      rm $SENTINEL"
fi
if [ -n "${XYZ_SHARDS:-}" ] && [ -n "$SHARDS" ] && [ "$SHARDS" != "$XYZ_SHARDS" ]; then
    fail "The sentinel describes $SHARDS shards and this run asked for $XYZ_SHARDS."
fi

# --checksum because a re-render writes the same shard names at much the same
# sizes: trusting size and modification time, rclone skipped every one of them
# and left the previous render in the bucket while reporting success.
FLAGS="--transfers=8 --checkers=16 --fast-list --retries=3 --checksum --stats=60s --stats-one-line"
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
    # copyto, not copy: the destination is a file name, not a directory. As a
    # two-step copy-then-rename this left an earlier run's manifest in place,
    # and the publisher read a stale one describing a shallower tree.
    hpc_rclone "manifest" copyto "$SENTINEL" "$(hpc_remote "$DEST/manifest.json")" \
        || fail "the manifest did not transfer"

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
