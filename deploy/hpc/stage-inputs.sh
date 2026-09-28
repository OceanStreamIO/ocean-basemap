#!/usr/bin/env bash
# =============================================================================
# Stage the build's inputs onto the cluster's scratch filesystem
# =============================================================================
# Three sources, none of which routes through this machine:
#
#   inputs/   the grids and vectors a previous build already assembled
#   tiles/    the finished raster archives the flat tree is composited from
#   dem       elevation cells, pulled straight from their public cloud bucket
#
# All of it goes to scratch, never to the backed-up project filesystem: it is a
# few hundred gigabytes of regenerable copies, and the project quota is shared.
#
#   ./deploy/hpc/stage-inputs.sh              # everything, resuming
#   ./deploy/hpc/stage-inputs.sh --pilot      # a capped sample, to prove the path
#   ./deploy/hpc/stage-inputs.sh --only dem   # one source
#   ./deploy/hpc/stage-inputs.sh --verify     # compare, transfer nothing
#
# `copy`, never `sync`: sync deletes what the destination has that the source
# does not, so an interrupted pull could not be resumed -- it would delete the
# progress instead of continuing it.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

BUILD_PREFIX="${BASEMAP_BUILD_PREFIX:-basemap-build}"
LIVE_PREFIX="${BASEMAP_S3_PREFIX:-basemap}"
PILOT_SIZE="${PILOT_SIZE:-500M}"
ONLY=""
PILOT=0
VERIFY=0

# The archives the flat tree composites. Taken from the live prefix, not from the
# build prefix: the build prefix holds whatever the last promotion left, which
# can be older than what the published map was actually made from -- and a stale
# archive here would both render a wrong map and invalidate every comparison
# against the published tree.
XYZ_ARCHIVES="land_relief.pmtiles land_relief_eu.pmtiles bathy_global.pmtiles
bathy_eu.pmtiles bathy_hr.pmtiles depth_contours.pmtiles"

while [ $# -gt 0 ]; do
    case "$1" in
        --only)    ONLY="${2:?--only needs inputs, tiles or dem}"; shift ;;
        --pilot)   PILOT=1 ;;
        --verify)  VERIFY=1 ;;
        -h|--help) sed -n '2,22p' "$0" >&2; exit 0 ;;
        *)         fail "Unknown argument: $1" ;;
    esac
    shift
done

require_site $HPC_SITE_VARS_XFER
require_hpc_key

FLAGS="--transfers=16 --checkers=32 --fast-list --retries=3 --low-level-retries=10 --stats=60s --stats-one-line"
[ "$PILOT" = "1" ] && FLAGS="$FLAGS --max-transfer=$PILOT_SIZE --cutoff-mode=soft"
OP="copy"
[ "$VERIFY" = "1" ] && OP="check --size-only --one-way"

wants() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

# --- the assembled inputs --------------------------------------------------
if wants inputs; then
    log "Assembled inputs → $HPC_DATA"
    # shellcheck disable=SC2086
    hpc_rclone "inputs" $OP "$(hpc_remote "$BUILD_PREFIX/inputs/")" "$HPC_DATA/" $FLAGS \
        || fail "the inputs did not transfer"
fi

# --- the raster archives ---------------------------------------------------
if wants tiles; then
    log "Raster archives → $HPC_DATA/gebco2026/tiles"
    INCLUDES=""
    for archive in $XYZ_ARCHIVES; do INCLUDES="$INCLUDES --include=$archive"; done
    # shellcheck disable=SC2086
    hpc_rclone "archives" $OP "$(hpc_remote "$LIVE_PREFIX/tiles/")" \
        "$HPC_DATA/gebco2026/tiles/" $INCLUDES $FLAGS \
        || fail "the archives did not transfer"
fi

# --- the elevation cells ---------------------------------------------------
if wants dem; then
    [ -n "${HPC_OPENDATA_REMOTE:-}" ] || fail "HPC_OPENDATA_REMOTE is not set (see site.env.example)"
    for product in glo30:copernicus-dem-30m glo90:copernicus-dem-90m; do
        subdir="${product%%:*}"; bucket="${product##*:}"
        log "Elevation cells ($subdir) → $HPC_DATA/gebco2026/work/land_dem/$subdir"
        # Straight from the public bucket to the cluster. Routing 135 GB through
        # here and back out again would cost hours and two copies of the bytes.
        # shellcheck disable=SC2086
        hpc_rclone "$subdir" $OP "${HPC_OPENDATA_REMOTE}:${bucket}/" \
            "$HPC_DATA/gebco2026/work/land_dem/$subdir/" \
            --include='*/*_DEM.tif' $FLAGS \
            || fail "$subdir did not transfer"
    done

    if [ "$VERIFY" = "0" ]; then
        log "Flattening the cell directories"
        # The bucket puts each cell in a directory of its own; the pipeline wants
        # them in one. Flattened here rather than by changing the pipeline,
        # because the same code has to render identically on both machines for
        # the comparison against the published tree to mean anything.
        hpc_ssh_login "python3 - '$HPC_DATA/gebco2026/work/land_dem' <<'PY'
import os, pathlib, sys

root = pathlib.Path(sys.argv[1])
for product in ('glo30', 'glo90'):
    base = root / product
    if not base.is_dir():
        continue
    moved = kept = 0
    for tif in list(base.glob('*/*.tif')):
        target = base / tif.name
        if target.exists():
            tif.unlink()
        else:
            os.replace(tif, target)
            moved += 1
    for stale in list(base.glob('*/')):
        try:
            stale.rmdir()
        except OSError:
            pass
    kept = len(list(base.glob('*.tif')))
    print(f'    {product}: {moved:,} flattened, {kept:,} cells present')
PY" || fail "could not flatten the cell directories"
    fi
fi

# --- what the render will refuse to start without --------------------------
log "Checking the inputs the render asserts on"
hpc_ssh_login "
    cd '$HPC_DATA/gebco2026' 2>/dev/null || { echo '    nothing staged yet'; exit 1; }
    for f in GEBCO_2026.tif tiles/depth_contours.pmtiles work/land/osm_land.fgb \
             work/lakes/lakes_all.fgb work/water/gsw/gsw_occurrence.vrt; do
        if [ -e \"\$f\" ]; then printf '    ok       %s\n' \"\$f\"
        else printf '    MISSING  %s\n' \"\$f\"; fi
    done
    for product in GLO-30 GLO-90; do
        if [ -e \"work/land_dem/\$product.tileList.txt\" ]; then
            printf '    ok       work/land_dem/%s.tileList.txt\n' \"\$product\"
        else
            printf '    MISSING  work/land_dem/%s.tileList.txt (the land relief needs it and cannot fetch it)\n' \"\$product\"
        fi
    done
    du -sh . 2>/dev/null | sed 's/^/    staged: /'
    df -Pk . | awk 'NR==2 {printf \"    %.0f GB free\n\", \$4/1048576}'
" || true
ok "Staging done"
log "Next: ./deploy/hpc/submit.sh"
