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

# Mosaic descriptions are deliberately not staged. A .vrt names its source files
# by path, and the ones in object storage were written on a machine with a
# different layout, so staging them would put a mosaic on the cluster that opens
# nothing. It would not error: GDAL reads a missing source as nodata, so the
# archive comes out with holes in it and nothing says so.
#
# The two the pipeline skips when they already exist -- the elevation and
# surface-water mosaics -- are now written with paths relative to themselves, so
# leaving them out means they are rebuilt correctly here. The EMODnet one is
# rebuilt unconditionally by its own step, which is why the check at the end of
# this script says whether that step needs to run.
NO_MOSAICS="--exclude=**/*.vrt --exclude=**/*.list"

# --- the assembled inputs --------------------------------------------------
if wants inputs; then
    log "Assembled inputs → $HPC_DATA"
    # shellcheck disable=SC2086
    HPC_XFER_WATCH="$HPC_DATA" \
    hpc_rclone "inputs" $OP "$(hpc_remote "$BUILD_PREFIX/inputs/")" "$HPC_DATA/" \
        $NO_MOSAICS $FLAGS \
        || fail "the inputs did not transfer"
fi

# --- the raster archives ---------------------------------------------------
if wants tiles; then
    log "Raster archives → $HPC_DATA/gebco2026/tiles"
    INCLUDES=""
    for archive in $XYZ_ARCHIVES; do INCLUDES="$INCLUDES --include=$archive"; done
    # shellcheck disable=SC2086
    HPC_XFER_WATCH="$HPC_DATA/gebco2026/tiles" \
    hpc_rclone "archives" $OP "$(hpc_remote "$LIVE_PREFIX/tiles/")" \
        "$HPC_DATA/gebco2026/tiles/" $INCLUDES $FLAGS \
        || fail "the archives did not transfer"
fi

# --- the elevation cells ---------------------------------------------------
if wants dem; then
    [ -n "${HPC_OPENDATA_REMOTE:-}" ] || fail "HPC_OPENDATA_REMOTE is not set (see site.env.example)"

    # Name the cells wanted, rather than filtering on a pattern. Each bucket holds
    # about 26,000 one-degree cells and the build wants a different subset of each
    # -- the fine product only where the regional bathymetry covers, the coarse
    # one everywhere else. Taking both whole is not a small waste: the fine cells
    # are ~27 MB each, so its bucket is about 720 GB against the 83 GB used, and
    # it would fill the staging quota with data the build then ignores.
    log "Working out which cells are wanted"
    LISTS="$(mktemp -d)"
    trap 'rm -rf "$LISTS"' EXIT
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        DEM_PY="$PROJECT_DIR/.venv/bin/python"
    elif command -v uv >/dev/null 2>&1; then
        DEM_PY="uv run --frozen --no-dev python"
    else
        DEM_PY="python3"
    fi
    # shellcheck disable=SC2086
    ( cd "$PROJECT_DIR" && $DEM_PY deploy/hpc/dem_manifest.py --out-dir "$LISTS" ) \
        || fail "could not work out the cell lists"

    hpc_ssh_login "mkdir -p '$HPC_ROOT/etc'" || fail "could not reach the cluster"
    for subdir in glo30 glo90; do
        hpc_ssh_login "cat > '$HPC_ROOT/etc/$subdir.files'" < "$LISTS/$subdir.files" \
            || fail "could not send the $subdir cell list"
    done

    for product in glo30:copernicus-dem-30m glo90:copernicus-dem-90m; do
        subdir="${product%%:*}"; bucket="${product##*:}"
        log "Elevation cells ($subdir) → $HPC_DATA/gebco2026/work/land_dem/$subdir"
        # Straight from the public bucket to the cluster. Routing this through
        # here and back out again would cost hours and two copies of the bytes.
        # shellcheck disable=SC2086
        HPC_XFER_WATCH="$HPC_DATA/gebco2026/work/land_dem/$subdir" \
        hpc_rclone "$subdir" $OP "${HPC_OPENDATA_REMOTE}:${bucket}/" \
            "$HPC_DATA/gebco2026/work/land_dem/$subdir/" \
            --files-from="$HPC_ROOT/etc/$subdir.files" $FLAGS \
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
    # The mosaics are not in this list on purpose: they are not staged, and the
    # second list below reports them as work still to do rather than as missing.
    for f in GEBCO_2026.tif tiles/depth_contours.pmtiles work/land/osm_land.fgb \
             work/lakes/lakes_all.fgb; do
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
    # Mosaics are rebuilt here rather than staged; say which ones are still to do,
    # because one of them changes what the bathymetry comes out as.
    for m in work/land_dem/glo90.vrt work/water/gsw/gsw_occurrence.vrt; do
        [ -e \"\$m\" ] && printf '    ok       %s (rebuilt)\n' \"\$m\" \
                        || printf '    to build %s (its step rebuilds it)\n' \"\$m\"
    done
    if [ -e work/emodnet/emodnet_mean.vrt ]; then
        printf '    ok       work/emodnet/emodnet_mean.vrt\n'
    else
        printf '    to build work/emodnet/emodnet_mean.vrt -- run with --with emodnet,\n'
        printf '             or the regional and high-resolution bathymetry are built from\n'
        printf '             the global grid alone, quietly.\n'
    fi
    du -sh . 2>/dev/null | sed 's/^/    staged: /'
    df -Pk . | awk 'NR==2 {printf \"    %.0f GB free\n\", \$4/1048576}'
" || true
ok "Staging done"
log "Next: ./deploy/hpc/submit.sh"
