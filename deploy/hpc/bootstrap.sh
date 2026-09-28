#!/usr/bin/env bash
# =============================================================================
# One-time (and idempotent) setup of the basemap environment on the cluster
# =============================================================================
# Creates the directory tree, captures the GDAL module environment, writes the
# command-line wrappers, builds tippecanoe, and writes the environment file
# every batch job sources.
#
#   ./deploy/hpc/bootstrap.sh              # everything, skipping what is done
#   ./deploy/hpc/bootstrap.sh --verify     # check only, change nothing
#   ./deploy/hpc/bootstrap.sh --tippecanoe # rebuild just tippecanoe
#
# Run it again after changing site.env: it rewrites the generated files.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

VERIFY_ONLY=0
ONLY_TIPPECANOE=0
TIPPECANOE_VERSION="${TIPPECANOE_VERSION:-2.82.0}"
TIPPECANOE_SRC="${TIPPECANOE_SRC:-$HOME/work/tools/tippecanoe-src}"

while [ $# -gt 0 ]; do
    case "$1" in
        --verify)     VERIFY_ONLY=1 ;;
        --tippecanoe) ONLY_TIPPECANOE=1 ;;
        -h|--help)    sed -n '2,16p' "$0" >&2; exit 0 ;;
        *)            fail "Unknown argument: $1" ;;
    esac
    shift
done

require_cmd ssh tar
require_site $HPC_SITE_VARS_SLURM HPC_GDAL_MODULES HPC_GDAL_PY_MODULES HPC_GDAL_PYTHON_BIN \
             HPC_SQLITE_MODULE HPC_SQLITE_INCLUDE HPC_SQLITE_LIB
require_hpc_key

# The command-line tools the pipeline resolves through shutil.which(). The
# first six are compiled programs; the rest are scripts run by the interpreter
# the GDAL module was built against, which is not the one the pipeline uses.
GDAL_BINARIES="gdaldem gdalbuildvrt gdalwarp gdal_contour gdal_rasterize ogr2ogr gdalinfo ogrinfo"
GDAL_SCRIPTS="gdal_polygonize.py gdal_calc.py gdal_merge.py gdal2tiles.py"

# ---------------------------------------------------------------------------
if [ "$VERIFY_ONLY" = "0" ] && [ "$ONLY_TIPPECANOE" = "0" ]; then
    log "Directories under $HPC_ROOT and $HPC_STAGE"
    hpc_ssh_login "
        set -e
        mkdir -p '$HPC_ROOT'/{code,bin,etc,src,logs,state,tmp,wheelhouse}
        mkdir -p '$HPC_DATA' '$HPC_OUT' '$HPC_STAGE/tmp'
        # setgid, so everything stays group-owned and a colleague can clean up
        # after us on a shared allocation.
        chmod 2770 '$HPC_ROOT' '$HPC_STAGE' 2>/dev/null || true
        df -Pk '$HPC_STAGE' | awk 'NR==2 {printf \"    %.0f GB free on the staging filesystem\n\", \$4/1048576}'
    " || fail "Could not create the directory tree"
    ok "Directories ready"

    # -----------------------------------------------------------------------
    log "Capturing the GDAL module environment"
    # Captured once rather than loaded per invocation. The module command is a
    # shell function, and sourcing the module system inside each of many
    # thousands of gdal_polygonize calls is both slow and fragile. Diffing the
    # environment is the only way to learn what a module actually sets.
    hpc_ssh_login "
        set -e
        env | sort > '$HPC_ROOT/tmp/env.before'
        module load $(hpc_modules "$HPC_GDAL_MODULES")
        env | sort > '$HPC_ROOT/tmp/env.after'
        # Only the variables the module added or changed, as exports.
        comm -13 '$HPC_ROOT/tmp/env.before' '$HPC_ROOT/tmp/env.after' \
            | grep -E '^(PATH|LD_LIBRARY_PATH|LIBRARY_PATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|PKG_CONFIG_PATH|GDAL_DATA|PROJ_LIB|PROJ_DATA|PROJ_NETWORK|GEOTIFF_CSV|MANPATH|LMOD|MODULE|_LMFILES)' \
            | sed 's/^/export /; s/=/=\"/; s/\$/\"/' > '$HPC_ROOT/etc/gdal-env.sh'
        rm -f '$HPC_ROOT/tmp/env.before' '$HPC_ROOT/tmp/env.after'
        wc -l < '$HPC_ROOT/etc/gdal-env.sh' | sed 's/^/    /; s/\$/ variables captured/'

    " || fail "Could not capture the GDAL environment"
    ok "etc/gdal-env.sh written"

    log "Capturing the environment for the Python GDAL tools"
    # A second, separate environment, for two independent reasons.
    #
    # It is a different *build* of the same GDAL. The optimised build's Python
    # bindings do not load -- "undefined symbol: __intel_sse2_strcpy", because
    # they want a compiler runtime that is not on the library path -- while the
    # reference build's do. The reference build is also a debug build, which is
    # why it is not used for everything: the six compiled tools that do the
    # actual work stay on the optimised one, and only the handful of scripts use
    # this.
    #
    # And it carries PYTHONPATH, which must never reach the pipeline's own
    # virtual environment or that resolves against the module's global packages
    # instead of its own. Two files is how one process sees it and the other
    # does not.
    hpc_ssh_login "
        unset PYTHONPATH
        env | sort > '$HPC_ROOT/tmp/py.before'
        module load $(hpc_modules "$HPC_GDAL_PY_MODULES")
        env | sort > '$HPC_ROOT/tmp/py.after'
        comm -13 '$HPC_ROOT/tmp/py.before' '$HPC_ROOT/tmp/py.after' \
            | grep -E '^(PATH|LD_LIBRARY_PATH|LIBRARY_PATH|PYTHONPATH|GDAL_DATA|PROJ_LIB|PROJ_DATA|GEOTIFF_CSV)=' \
            | sed 's/^/export /; s/=/=\"/; s/\$/\"/' > '$HPC_ROOT/etc/gdal-python.sh'
        rm -f '$HPC_ROOT/tmp/py.before' '$HPC_ROOT/tmp/py.after'
        grep -c . '$HPC_ROOT/etc/gdal-python.sh' | sed 's/^/    /; s/\$/ variables captured/'
    " || fail "Could not capture the Python GDAL environment"
    ok "etc/gdal-python.sh written"

    # -----------------------------------------------------------------------
    log "Wrapper scripts in $HPC_BIN"
    # Why wrappers at all, for both kinds of tool:
    #
    # The pipeline's own GDAL is the one inside its rasterio and fiona wheels,
    # and it is a different version from the module's. The module exports
    # GDAL_DATA and PROJ_LIB, and those are read by whichever GDAL is loaded --
    # so leaving them set would hand the wheel's GDAL another version's
    # projection database and change coordinate handling with no error at all.
    # The batch environment therefore unsets them, and the command-line tools
    # can no longer inherit them: each wrapper restores what it needs.
    #
    # The script tools have a second problem. They carry a shebang naming the
    # interpreter the module was built against, and they need that
    # interpreter's site-packages on PYTHONPATH -- while the pipeline's own
    # virtual environment requires PYTHONPATH to be unset. The wrapper invokes
    # that interpreter explicitly rather than trusting the shebang, which also
    # survives the shebang being wrong.
    hpc_ssh_login "
        set -e
        mkdir -p '$HPC_BIN'
        for tool in $GDAL_BINARIES; do
            real=\"\$(module load $(hpc_modules "$HPC_GDAL_MODULES") >/dev/null 2>&1; command -v \$tool || true)\"
            if [ -z \"\$real\" ]; then echo \"    MISSING: \$tool\" >&2; continue; fi
            printf '%s\n' '#!/bin/bash' \
                          '# Generated by deploy/hpc/bootstrap.sh -- do not edit.' \
                          \". '$HPC_ROOT/etc/gdal-env.sh'\" \
                          \"exec \$real \\\"\\\$@\\\"\" > '$HPC_BIN'/\$tool
            chmod +x '$HPC_BIN'/\$tool
        done
        for tool in $GDAL_SCRIPTS; do
            # From the reference build, matching the bindings they import.
            real=\"\$(module load $(hpc_modules "$HPC_GDAL_PY_MODULES") >/dev/null 2>&1; command -v \$tool || true)\"
            if [ -z \"\$real\" ]; then echo \"    MISSING: \$tool\" >&2; continue; fi
            printf '%s\n' '#!/bin/bash' \
                          '# Generated by deploy/hpc/bootstrap.sh -- do not edit.' \
                          \". '$HPC_ROOT/etc/gdal-python.sh'\" \
                          'unset PYTHONHOME' \
                          \"exec '$HPC_GDAL_PYTHON_BIN' \$real \\\"\\\$@\\\"\" > '$HPC_BIN'/\$tool
            chmod +x '$HPC_BIN'/\$tool
        done
        ls '$HPC_BIN' | wc -l | sed 's/^/    /; s/\$/ wrappers/'
    " || fail "Could not write the wrappers"
    ok "Wrappers written"
fi

# ---------------------------------------------------------------------------
if [ "$VERIFY_ONLY" = "0" ]; then
    log "tippecanoe $TIPPECANOE_VERSION"
    if hpc_ssh_login "test -x '$HPC_BIN/tippecanoe'" 2>/dev/null && [ "$ONLY_TIPPECANOE" = "0" ]; then
        info "already built; --tippecanoe forces a rebuild"
    else
        [ -d "$TIPPECANOE_SRC" ] || fail "No tippecanoe source at $TIPPECANOE_SRC.
  Set TIPPECANOE_SRC, or clone it: git clone --branch $TIPPECANOE_VERSION \\
      https://github.com/felt/tippecanoe.git"
        # A clean export, not the working tree: a tree that has been built in
        # carries object files that confuse make on another architecture.
        log "Sending the source"
        if git -C "$TIPPECANOE_SRC" rev-parse --git-dir >/dev/null 2>&1; then
            git -C "$TIPPECANOE_SRC" archive --format=tar.gz --prefix=tippecanoe/ HEAD \
                | hpc_ssh_login "mkdir -p '$HPC_ROOT/src' && tar -xzf - -C '$HPC_ROOT/src'"
        else
            ( cd "$TIPPECANOE_SRC" && tar -czf - --exclude='*.o' --exclude='.git' \
                  --transform='s,^\.,tippecanoe,' . 2>/dev/null \
              || tar -czf - --exclude='*.o' --exclude='.git' . ) \
                | hpc_ssh_login "mkdir -p '$HPC_ROOT/src/tippecanoe' && tar -xzf - -C '$HPC_ROOT/src/tippecanoe'"
        fi
        log "Building (this takes a few minutes)"
        # -Wl,-rpath so the finished binaries find the site's SQLite without the
        # module being loaded. Batch jobs do not inherit login-shell modules,
        # and a tool that needs one is a tool that fails on a compute node.
        hpc_ssh_login "
            set -e
            module load $(hpc_modules "$HPC_SQLITE_MODULE")
            cd '$HPC_ROOT/src/tippecanoe'
            make -j8 \
                CXXFLAGS='-std=c++17 -O3 -I$HPC_SQLITE_INCLUDE' \
                LDFLAGS='-L$HPC_SQLITE_LIB -Wl,-rpath,$HPC_SQLITE_LIB' >/dev/null
            make install PREFIX='$HPC_ROOT' >/dev/null
        " || fail "tippecanoe did not build"
        ok "tippecanoe built"
    fi

    # -----------------------------------------------------------------------
    log "etc/basemap-env.sh"
    # Sourced by every batch job. One place, so a fix reaches all five of them.
    hpc_ssh_login "cat > '$HPC_ROOT/etc/basemap-env.sh'" <<ENVEOF
# Generated by deploy/hpc/bootstrap.sh -- do not edit.
# Sourced by every basemap batch job.
set -euo pipefail

module load $(hpc_modules "$HPC_MODULES")

# The Python module exports PYTHONHOME and PYTHONPATH, and both shadow the
# virtual environment's own site-packages. Unset after activating, every time.
. '$HPC_VENV/bin/activate'
unset PYTHONHOME PYTHONPATH

# The wrappers and tippecanoe first: the pipeline finds its tools with
# shutil.which(), which reads this process's PATH and nothing else.
export PATH='$HPC_BIN':"\$PATH"

# Not inherited by the command-line tools -- each wrapper sets its own. Left
# set, the module's projection database would be picked up by the different
# GDAL inside the rasterio wheel, silently.
unset GDAL_DATA PROJ_LIB PROJ_DATA

export BASEMAP_DATA_DIR='$HPC_DATA'
# No route to the internet from a compute node. Under this, a missing input is
# an error naming itself in the first second rather than a timeout per file.
export BASEMAP_OFFLINE=1

# Cores actually allocated, not cores on the node.
if [ -n "\${SLURM_CPUS_PER_TASK:-}" ]; then
    export BASEMAP_TILE_PROCESSES="\$SLURM_CPUS_PER_TASK"
fi
# GDAL's own threading would fight the worker pool for the same cores.
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

# Node-local scratch. tippecanoe writes tens of gigabytes of temporary files
# and the multi-layer builds use a temporary directory beside their output, so
# without this both land on shared storage.
if [ -n "\${SLURM_JOB_ID:-}" ] && [ -d /scratch/tmp ]; then
    export TMPDIR="/scratch/tmp/\$SLURM_JOB_ID"
elif [ -n "\${SLURM_JOB_ID:-}" ]; then
    export TMPDIR="$HPC_STAGE/tmp/\$SLURM_JOB_ID"
fi
mkdir -p "\${TMPDIR:-/tmp}"
ENVEOF
    ok "etc/basemap-env.sh written"
fi

# ---------------------------------------------------------------------------
log "Verifying"
hpc_ssh_login "
    export PATH='$HPC_BIN':\$PATH
    broken=0
    for tool in $GDAL_BINARIES $GDAL_SCRIPTS tippecanoe tile-join; do
        if ! command -v \$tool >/dev/null 2>&1; then
            echo \"    MISSING   \$tool\"; broken=1; continue
        fi
        # Not every tool has --version or --help; tile-join rejects both and
        # prints its usage. So the test is 'says something sensible and does not
        # traceback', not 'accepts a particular flag'.
        out=\"\$(\$tool --version 2>&1 || \$tool --help 2>&1 || true)\"
        case \"\$out\" in
            *Traceback*|*\"No module named\"*|*\"cannot open shared object\"*|\"\")
                echo \"    BROKEN    \$tool: \$(printf '%s' \"\$out\" | tail -1)\"; broken=1 ;;
            *)  echo \"    ok        \$tool\" ;;
        esac
    done
    echo \"    gdal      \$(gdalinfo --version 2>/dev/null || echo unknown)\"
    echo \"    tippecanoe \$(tippecanoe --version 2>&1 | head -1)\"
    ogrinfo --formats 2>/dev/null | grep -q FlatGeobuf \
        && echo '    ok        FlatGeobuf driver' \
        || { echo '    MISSING   FlatGeobuf driver'; broken=1; }
    if [ -f '$HPC_VENV/bin/activate' ]; then
        . '$HPC_ROOT/etc/basemap-env.sh' 2>/dev/null || true
        python3 - <<'PY'
import importlib
for name in ('rasterio', 'fiona', 'shapely', 'PIL', 'numpy', 'mapbox_vector_tile',
             'pyclipper', 'boto3', 'basemap'):
    try:
        module = importlib.import_module(name)
        version = getattr(module, '__version__', '')
        extra = ''
        if name == 'rasterio':
            extra = f\"  (bundled GDAL {module.__gdal_version__})\"
        print(f'    ok        {name} {version}{extra}')
    except Exception as exc:
        print(f'    BROKEN    {name}: {exc}')
PY
    else
        echo '    MISSING   virtual environment -- run ./deploy/hpc/wheelhouse.sh'
        broken=1
    fi
    exit \$broken
" && ok "Environment is ready" || fail "Environment is incomplete (see above)"
