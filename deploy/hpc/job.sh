#!/usr/bin/env bash
# =============================================================================
# job.sh — the cluster build, driven from inside the basemap-hpc Job
# =============================================================================
# A state machine rather than one long script, because the parts run on three
# different schedulers and the pod may outlive none of them:
#
#   stage   → object storage and a public bucket into cluster scratch
#   submit  → the render chain on the compute cluster
#   export  → scratch back out to object storage, gated on a sentinel
#
# PHASE selects where to start. Every phase is idempotent: staging copies
# (never syncs), submission adopts a queued job of the same name instead of
# submitting a second one, and the export resumes.
# =============================================================================
set -euo pipefail

: "${CODE_REV:?CODE_REV must name the code tarball to run}"
PHASE="${PHASE:-all}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
SHARDS="${XYZ_SHARDS:-4}"

BUILD=s3://oceanstream-bucket/basemap-build
export HOME=/home/hpc
export UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools UV_TOOL_BIN_DIR=/tmp/uv-bin
export PATH="/tmp/uv-bin:$PATH"
export AWS_EC2_METADATA_DISABLED=true
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"

echo "=== Step 1: tools ==="
# openssh-client is not in the base image, and this pod is nothing but an ssh
# client. Installed rather than baked into an image of our own, which would mean
# a registry, a pull secret and a scope-map entry for a 200m pod.
apt-get -qq update >/dev/null 2>&1
apt-get -qq install -y --no-install-recommends openssh-client >/dev/null 2>&1
uv tool install --quiet awscli
echo "  $(ssh -V 2>&1) · run $RUN_ID · phase $PHASE · $SHARDS shards"

echo "=== Step 2: code $CODE_REV ==="
rm -rf /tmp/src && mkdir -p /tmp/src
aws --endpoint-url "${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}" s3 cp \
    --only-show-errors "$BUILD/code/ocean-basemap-$CODE_REV.tar.gz" /tmp/src/code.tar.gz
tar -xzf /tmp/src/code.tar.gz -C /tmp/src
cd /tmp/src/ocean-basemap
HPC=./deploy/hpc

echo "=== Step 3: reachability ==="
# Fail in seconds with the reason, rather than inside a transfer. The key, the
# host keys and both clusters are checked before anything is asked of them.
[ -r "$HPC_SSH_KEY" ] || { echo "no readable key at $HPC_SSH_KEY" >&2; exit 1; }
"$HPC"/status.sh >/dev/null 2>&1 || true
bash -c ". $HPC/lib.sh && require_site && require_hpc_key \
         && hpc_ssh_login hostname && hpc_ssh_xfer hostname" \
    || { echo "cannot reach the cluster -- check the site ConfigMap and the key" >&2; exit 1; }

run_phase() {
    case "$1" in
        stage)  echo "### stage  $(date -Is)";  "$HPC"/stage-inputs.sh ;;
        submit) echo "### submit $(date -Is)";  "$HPC"/submit.sh --shards "$SHARDS" ${WITH:-} ;;
        export) echo "### export $(date -Is)";  RUN_ID="$RUN_ID" "$HPC"/export-tiles.sh --wait ;;
    esac
}

case "$PHASE" in
    all)    for phase in stage submit export; do run_phase "$phase"; done ;;
    stage|submit|export) run_phase "$PHASE" ;;
    *)      echo "unknown PHASE: $PHASE (stage, submit, export or all)" >&2; exit 1 ;;
esac

echo "=== Done $(date -Is) ==="
if [ "$PHASE" = "all" ] || [ "$PHASE" = "export" ]; then
    echo
    echo "Publish the render with:"
    echo "    RUN_ID=$RUN_ID ./deploy/edito/xyz-publish/run.sh"
fi
