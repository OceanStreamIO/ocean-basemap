#!/usr/bin/env bash
# =============================================================================
# Stage the build's inputs onto the cluster's scratch filesystem
# =============================================================================
# Three sources, each streamed from object storage through this machine and
# over SSH to the transfer node -- the cluster may not connect out itself:
#
#   inputs/   the grids and vectors a previous build already assembled
#   tiles/    the finished raster archives the flat tree is composited from
#   dem       elevation cells, from their public cloud bucket
#
# All of it goes to scratch, never to the backed-up project filesystem: it is a
# few hundred gigabytes of regenerable copies, and the project quota is shared.
#
#   ./deploy/hpc/stage-inputs.sh              # everything, resuming
#   ./deploy/hpc/stage-inputs.sh --pilot      # one small archive, to prove the path
#   ./deploy/hpc/stage-inputs.sh --only dem   # one source
#   ./deploy/hpc/stage-inputs.sh --verify     # compare, transfer nothing
#
# Copy, never sync: nothing on the cluster is deleted, so an interrupted pull
# resumes where it stopped. A file is written under .part and renamed only once
# it has its full size, so a half-written file never passes for a staged one.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

BUILD_PREFIX="${BASEMAP_BUILD_PREFIX:-basemap-build}"
LIVE_PREFIX="${BASEMAP_S3_PREFIX:-basemap}"
ONLY=""
PILOT=0
VERIFY=0

# The archives the flat tree composites. Taken from the live prefix, not from the
# build prefix: the build prefix holds whatever the last promotion left, which
# can be older than what the published map was actually made from -- and a stale
# archive here would both render a wrong map and invalidate every comparison
# against the published tree.
# lake_relief and bathy_gmrt were missing here while the tree read them, which
# would have rendered a tree with flat lakes and no swath outside Europe.
XYZ_ARCHIVES="land_relief.pmtiles land_relief_eu.pmtiles bathy_global.pmtiles
bathy_eu.pmtiles bathy_gmrt.pmtiles bathy_hr.pmtiles depth_contours.pmtiles
lake_relief.pmtiles"
# The globe pair. The step renders whichever of the two is absent, so without the
# one a run is not rebuilding it would render that too, and throw it away.
GLOBE_ARCHIVES="bathy_geo.pmtiles land_geo.pmtiles"

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

require_site
require_hpc_key

VERIFY_FLAG=""
[ "$VERIFY" = "1" ] && VERIFY_FLAG="--verify"
# A pilot proves the path end to end on one small archive and nothing else.
if [ "$PILOT" = "1" ]; then
    ONLY=tiles
    XYZ_ARCHIVES="bathy_global.pmtiles"
    GLOBE_ARCHIVES=""
fi

# HPC_STAGE_SKIP names sources to leave out, e.g. "dem" for a run that only
# rebuilds the bathymetry: the elevation cells are 135 GB of scratch that only
# the land relief reads.
wants() {
    case " ${HPC_STAGE_SKIP:-} " in *" $1 "*) return 1 ;; esac
    [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]
}

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
NO_MOSAICS="--exclude *.vrt --exclude *.list"

# Archives this run must not take from the published map, as REBUILD does for
# the datalab build. They are cleared on scratch and left out of the pull from
# the live prefix, so the render builds them afresh -- or the inputs bring them,
# which is how an archive the datalab has already built (the lake beds) reaches
# the cluster without its sources.
#
# Cleared once per run, not once per call: staging again after the render has
# started must not delete what it built. The same idiom as the datalab build's
# .cleared marker, keyed on the run for the same reason.
REBUILD="${HPC_REBUILD:-}"
for name in $REBUILD; do
    case "$name" in *[!a-z0-9_]*) fail "HPC_REBUILD names archives, e.g. bathy_hr, not '$name'" ;; esac
done
if [ -n "$REBUILD" ] && [ "$VERIFY" = "0" ] && [ "$PILOT" = "0" ] && { wants inputs || wants tiles; }; then
    case "${RUN_ID:-}" in
        ""|*[!A-Za-z0-9._-]*) fail "HPC_REBUILD needs a RUN_ID (letters, digits, . _ -): it keys the once-per-run clear" ;;
    esac
    files=""
    for name in $REBUILD; do files="$files $name.pmtiles $name.stage.mbtiles"; done
    log "Clearing what this run rebuilds:$(printf ' %s' $REBUILD)"
    hpc_ssh_login "
        set -e
        mkdir -p '$HPC_STATE' '$HPC_DATA/gebco2026/tiles'
        marker='$HPC_STATE/cleared-$RUN_ID'
        if [ -f \"\$marker\" ]; then echo '    already cleared for run $RUN_ID'; exit 0; fi
        cd '$HPC_DATA/gebco2026/tiles'
        rm -fv -- $files | sed 's/^/    /'
        echo '$REBUILD' > \"\$marker\"
    " || fail "could not clear the archives this run rebuilds"
fi

# --- the assembled inputs --------------------------------------------------
if wants inputs; then
    log "Assembled inputs → $HPC_DATA"
    # shellcheck disable=SC2086
    hpc_xfer pull --src "$(hpc_s3 "$BUILD_PREFIX/inputs/")" --dest "$HPC_DATA" \
        $NO_MOSAICS $VERIFY_FLAG \
        || fail "the inputs did not transfer"
fi

# --- the raster archives ---------------------------------------------------
if wants tiles; then
    log "Raster archives → $HPC_DATA/gebco2026/tiles"
    INCLUDES=""
    for archive in $XYZ_ARCHIVES $GLOBE_ARCHIVES; do
        case " $REBUILD " in
            *" ${archive%.pmtiles} "*) info "$archive: made by this run, not taken from the live map"; continue ;;
        esac
        INCLUDES="$INCLUDES --include $archive"
    done
    # With no --include at all the pull would take the whole live prefix.
    [ -n "$INCLUDES" ] || fail "every archive is in HPC_REBUILD, so there is nothing to take from the live map"
    # Only the top of the prefix. Below it are copies under the same names --
    # tiles/v/ holds the digest-named ones, tiles/water-a/ to water-f/ the palette
    # candidates -- and a name pattern alone pulled all seven of every archive.
    # shellcheck disable=SC2086
    hpc_xfer pull --src "$(hpc_s3 "$LIVE_PREFIX/tiles/")" \
        --dest "$HPC_DATA/gebco2026/tiles" --exclude '*/*' $INCLUDES $VERIFY_FLAG \
        || fail "the archives did not transfer"
fi

# --- the elevation cells ---------------------------------------------------
if wants dem; then
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

    for product in glo30:copernicus-dem-30m:GLO-30 glo90:copernicus-dem-90m:GLO-90; do
        subdir="${product%%:*}"; rest="${product#*:}"; bucket="${rest%%:*}"; name="${rest##*:}"
        log "Elevation cells ($subdir) → $HPC_DATA/gebco2026/work/land_dem/$subdir"
        # The bucket puts each cell in a directory of its own and the pipeline
        # wants them in one, so they land flat: --flatten drops the directory.
        # shellcheck disable=SC2086
        hpc_xfer pull --src "s3://$bucket/" --opendata --flatten \
            --files-from "$LISTS/$subdir.files" \
            --dest "$HPC_DATA/gebco2026/work/land_dem/$subdir" $VERIFY_FLAG \
            || fail "$subdir did not transfer"
        # The cell list the land relief reads offline, which it cannot fetch there.
        if [ "$VERIFY" = "0" ]; then
            curl -fsS "https://$bucket.s3.amazonaws.com/tileList.txt" \
                | hpc_ssh_xfer "cat > '$HPC_DATA/gebco2026/work/land_dem/$name.tileList.txt'" \
                || fail "could not send the $name tile list"
        fi
    done

    if [ "$VERIFY" = "0" ]; then
        log "Flattening any cell directories left by an older staging"
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
    for f in GEBCO_2026.tif tiles/depth_contours.pmtiles work/land/osm_land.fgb work/land/rivers.fgb \
             work/lakes/lakes_all.fgb work/land/land_10m.geojson work/ice/ice_10m.geojson \
             work/cover/esa_worldcover_grid.fgb; do
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
    for m in work/land_dem/glo90.vrt work/cover/worldcover.vrt work/water/gsw/gsw_occurrence.vrt \
             work/water/gsw/gsw_occurrence_ov0.vrt work/water/gsw/gsw_occurrence_ov1.vrt \
             work/water/gsw/gsw_occurrence_ov2.vrt; do
        [ -e \"\$m\" ] && printf '    ok       %s (rebuilt)\n' \"\$m\" \
                        || printf '    to build %s (its step rebuilds it)\n' \"\$m\"
    done
    if [ -e work/emodnet/emodnet_mean.vrt ]; then
        printf '    ok       work/emodnet/emodnet_mean.vrt\n'
    else
        printf '    to build work/emodnet/emodnet_mean.vrt (the raster job builds it from the\n'
        printf '             staged chunks; without it both regional archives would be\n'
        printf '             built from the global grid alone, quietly)\n'
    fi
    du -sh . 2>/dev/null | sed 's/^/    staged: /'
    df -Pk . | awk 'NR==2 {printf \"    %.0f GB free\n\", \$4/1048576}'
" || true
ok "Staging done"
log "Next: ./deploy/hpc/submit.sh"
