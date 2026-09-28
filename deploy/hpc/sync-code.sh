#!/usr/bin/env bash
# =============================================================================
# Send this repository's source to the cluster
# =============================================================================
# The compute cluster cannot reach a git remote, so source arrives as a tar
# stream. rsync is not used: it is absent from the container this runs in.
#
# Only source is sent. The virtual environment, the staged inputs and the
# rendered output on the cluster are left alone.
#
#   ./deploy/hpc/sync-code.sh              # send and reinstall the package
#   ./deploy/hpc/sync-code.sh --dry-run    # list what would go
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,14p' "$0" >&2; exit 0; }

require_cmd git tar
PATHS="src pyproject.toml uv.lock color-ramps lake-sources.json"
DEST="$HPC_ROOT/code"

cd "$PROJECT_DIR"
if [ "$DRY_RUN" = "1" ]; then
    log "Would send to $DEST"
    # shellcheck disable=SC2086
    tar -cf - --exclude='__pycache__' --exclude='*.pyc' $PATHS | tar -tf - | head -20 >&2
    exit 0
fi

require_site
require_hpc_key
REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
git diff --quiet HEAD -- src 2>/dev/null || DIRTY=" (with uncommitted changes in src/)"

log "Sending $REV$DIRTY to $DEST"
# shellcheck disable=SC2086
hpc_send_tree "$PROJECT_DIR" "$DEST" --exclude='__pycache__' --exclude='*.pyc' $PATHS

# Reinstalled without dependencies: the wheelhouse already placed those, and
# resolving again would want an index that is not reachable.
hpc_ssh_login "
    set -e
    . '$HPC_VENV/bin/activate'
    unset PYTHONHOME PYTHONPATH
    cd '$DEST'
    python -m pip install --no-index --no-deps --quiet -e . 2>&1 | tail -2
    echo '$REV  '\$(date -u +%FT%TZ) >> '$DEST/.synced-revisions'
    python -c 'import basemap; print(\"    basemap package importable\")'
" || fail "could not install the package"
ok "Synced at $REV"
