#!/usr/bin/env bash
# =============================================================================
# Build the wheelhouse here, install the environment there
# =============================================================================
# The compute cluster has no route to a package index, so the dependency
# closure is downloaded on this machine and sent as a tar stream.
#
#   ./deploy/hpc/wheelhouse.sh              # build, send, install
#   ./deploy/hpc/wheelhouse.sh --local-only # build here, send nothing
#   ./deploy/hpc/wheelhouse.sh --recreate   # discard the remote venv first
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

# The interpreter on the cluster. Two things depend on getting this right, and
# both fail quietly: the wheel tags have to match it, and a virtual environment
# built by a different interpreter cannot load the wheels at all.
PY_VERSION="${HPC_PY_VERSION:-3.12}"
# The oldest glibc the wheels must load against. The build host here is newer
# than the cluster, so an unconstrained download fetches wheels that install
# fine on this machine and fail over there.
PLATFORMS="${HPC_WHEEL_PLATFORMS:-manylinux_2_28_x86_64 manylinux2014_x86_64}"

LOCAL_ONLY=0
RECREATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --local-only) LOCAL_ONLY=1 ;;
        --recreate)   RECREATE=1 ;;
        -h|--help)    sed -n '2,12p' "$0" >&2; exit 0 ;;
        *)            fail "Unknown argument: $1" ;;
    esac
    shift
done

require_cmd uv tar
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "Resolving the dependency closure for Python $PY_VERSION"
# Resolved *for* that version, not for this one. Markers such as
# python_version < "3.13" select different packages, and a lock resolved here
# would quietly omit or substitute some of them.
( cd "$PROJECT_DIR" && uv export --frozen --no-dev --no-hashes --no-emit-project \
      --python "$PY_VERSION" -o "$WORK/requirements.txt" ) \
    || fail "uv export failed"
info "$(wc -l < "$WORK/requirements.txt" | tr -d ' ') requirements"

log "Fetching wheels"
# Downloaded *by* a real interpreter of that version. pip run from a newer one
# silently drops packages whose markers exclude the newer version, which is how
# a wheelhouse comes out missing something the cluster then cannot install.
uv python install "$PY_VERSION" >/dev/null 2>&1 || true
uv venv --python "$PY_VERSION" "$WORK/venv" >/dev/null || fail "could not make a $PY_VERSION venv"
PLATFORM_ARGS=""
for platform in $PLATFORMS; do PLATFORM_ARGS="$PLATFORM_ARGS --platform $platform"; done
# shellcheck disable=SC2086
"$WORK/venv/bin/python" -m pip download \
    -r "$WORK/requirements.txt" -d "$WORK/wheelhouse" \
    --only-binary=:all: --python-version "$PY_VERSION" --implementation cp \
    --abi "cp${PY_VERSION//./}" $PLATFORM_ARGS 2>&1 | tail -3 \
    || fail "pip download failed"

COUNT="$(find "$WORK/wheelhouse" -name '*.whl' | wc -l | tr -d ' ')"
SIZE="$(du -sh "$WORK/wheelhouse" | cut -f1)"
ok "$COUNT wheels, $SIZE"

# The packages with compiled extensions, which are the ones that would fail on a
# glibc mismatch. Named so a missing one is obvious now rather than on the
# cluster.
for package in rasterio fiona shapely numpy pillow mapbox_vector_tile pyclipper protobuf; do
    found="$(find "$WORK/wheelhouse" -iname "${package}-*.whl" | head -1)"
    if [ -n "$found" ]; then
        info "$(basename "$found")"
    else
        warn "no wheel for $package"
    fi
done

if [ "$LOCAL_ONLY" = "1" ]; then
    cp -r "$WORK/wheelhouse" "$PROJECT_DIR/wheelhouse"
    cp "$WORK/requirements.txt" "$PROJECT_DIR/wheelhouse/requirements.txt"
    ok "Left in $PROJECT_DIR/wheelhouse (gitignored)"
    exit 0
fi

require_site $HPC_SITE_VARS_SLURM
require_hpc_key
cp "$WORK/requirements.txt" "$WORK/wheelhouse/requirements.txt"

log "Sending $SIZE to $HPC_ROOT/wheelhouse"
( cd "$WORK" && tar -czf - wheelhouse ) \
    | hpc_ssh_login "mkdir -p '$HPC_ROOT' && tar -xzf - -C '$HPC_ROOT'" \
    || fail "could not send the wheelhouse"
ok "Sent"

log "Installing"
# On the login node, not a transfer node: the environment has to be built
# against the same module stack the compute nodes will load.
#
# Never pipe or subshell `module` -- it is a shell function, and its changes to
# PATH are lost with the subshell, leaving the system interpreter in place and
# the whole install silently against the wrong Python.
hpc_ssh_login "
    set -e
    module load $(hpc_modules "$HPC_MODULES")
    python3 -V
    python3 -V 2>&1 | grep -q ' $PY_VERSION\.' || { echo 'wrong interpreter'; exit 3; }
    [ '$RECREATE' = '1' ] && rm -rf '$HPC_VENV'
    if [ -d '$HPC_VENV' ] && ! '$HPC_VENV/bin/python' -V 2>&1 | grep -q ' $PY_VERSION\.'; then
        echo '    discarding a venv built by another interpreter'
        rm -rf '$HPC_VENV'
    fi
    mkdir -p '$(dirname "$HPC_VENV")'
    [ -d '$HPC_VENV' ] || python3 -m venv '$HPC_VENV'
    . '$HPC_VENV/bin/activate'
    unset PYTHONHOME PYTHONPATH
    python -m pip install --quiet --no-index \
        --find-links='$HPC_ROOT/wheelhouse' pip setuptools wheel 2>/dev/null || true
    python -m pip install --no-index --find-links='$HPC_ROOT/wheelhouse' \
        -r '$HPC_ROOT/wheelhouse/requirements.txt' 2>&1 | tail -5
    echo \"    installed \$(python -m pip list --format=freeze | wc -l | tr -d ' ') packages\"
" || fail "install failed"
ok "Environment installed"
log "Next: ./deploy/hpc/sync-code.sh && ./deploy/hpc/bootstrap.sh"
