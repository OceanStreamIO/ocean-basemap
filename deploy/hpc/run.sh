#!/usr/bin/env bash
# =============================================================================
# run.sh — (re)start the cluster build driver as a Job
# =============================================================================
#   ./deploy/hpc/run.sh                       # stage, submit, export
#   PHASE=submit ./deploy/hpc/run.sh          # one phase
#   XYZ_SHARDS=8 ./deploy/hpc/run.sh
#   ./deploy/hpc/run.sh --dry-run
#
# The site values come from the basemap-hpc-site ConfigMap, which is created
# from deploy/hpc/site.env and is NOT in this repository. Create it once with:
#
#   kubectl -n project-oceanstream create configmap basemap-hpc-site \
#       --from-env-file=deploy/hpc/site.env
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
HPC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HPC_DIR/../.." && pwd)"
CODE_REV="${CODE_REV:-$(git -C "$REPO" rev-parse --short HEAD)}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
SHARDS="${XYZ_SHARDS:-4}"
MAX_ZOOM="${XYZ_MAX_ZOOM:-12}"
PHASE="${PHASE:-all}"
BUCKET=s3://oceanstream-bucket/basemap-build
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || err "kubectl not found"
command -v aws >/dev/null || err "aws CLI not found"

KUBECTL=(kubectl -n "$NAMESPACE")
APPLY=("${KUBECTL[@]}" apply)
(( DRY_RUN )) && APPLY+=(--dry-run=server) && info "DRY RUN — nothing will be changed"

info "Prerequisites"
"${KUBECTL[@]}" get secret oceanstream-hpc-ssh >/dev/null 2>&1 \
    || err "Secret oceanstream-hpc-ssh is missing (it holds id_ed25519 and known_hosts)"
ok "SSH secret present"
"${KUBECTL[@]}" get configmap basemap-hpc-site >/dev/null 2>&1 \
    || err "ConfigMap basemap-hpc-site is missing. Create it from your site.env:
    kubectl -n $NAMESPACE create configmap basemap-hpc-site --from-env-file=deploy/hpc/site.env"
ok "site ConfigMap present"

# The driver moves no bulk data, but it does fetch its own code from object
# storage, so it needs credentials. Checked here because the failure otherwise
# lands two minutes into a pod as "Unable to locate credentials", which reads
# like a broken bucket -- and because every script here was first run from the
# datalab pod, where the credentials are already in the environment and their
# absence in the Job is invisible.
for key in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    "${KUBECTL[@]}" get secret jupyter-python-568282-secrets3 \
        -o "go-template={{index .data \"$key\"}}" >/dev/null 2>&1 \
        || err "Secret jupyter-python-568282-secrets3 has no $key; the driver cannot read its code."
done
ok "S3 credentials present"

# The site values the scripts read with no default. A missing one otherwise
# surfaces as a connection to nowhere.
for key in HPC_USER HPC_LOGIN_HOST HPC_XFER_HOST HPC_ACCOUNT HPC_PROJ HPC_SCRATCH \
           HPC_PARTITION HPC_QOS HPC_RCLONE_REMOTE HPC_RCLONE_BUCKET \
           HPC_XFER_RCLONE_CMD HPC_DT_SLURM_CONF; do
    value="$("${KUBECTL[@]}" get configmap basemap-hpc-site \
             -o "go-template={{index .data \"$key\"}}" 2>/dev/null)"
    case "$value" in ""|"<no value>") err "basemap-hpc-site is missing $key" ;; esac
done
ok "site values complete"

info "Code $CODE_REV"
tarball="ocean-basemap-$CODE_REV.tar.gz"
if aws --endpoint-url "$ENDPOINT" s3 ls "$BUCKET/code/$tarball" >/dev/null 2>&1; then
    ok "already uploaded"
elif (( DRY_RUN )); then
    ok "would upload $tarball"
else
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    git -C "$REPO" archive --format=tar.gz --prefix=ocean-basemap/ -o "$tmp/$tarball" "$CODE_REV"
    aws --endpoint-url "$ENDPOINT" s3 cp --only-show-errors "$tmp/$tarball" "$BUCKET/code/$tarball"
    ok "uploaded $tarball"
fi

info "Script"
"${KUBECTL[@]}" create configmap basemap-hpc-driver-script \
    --from-file="$HPC_DIR/job.sh" --dry-run=client -o yaml | "${APPLY[@]}" -f -

info "Job (phase $PHASE, run $RUN_ID, $SHARDS shards, z0-$MAX_ZOOM)"
if (( ! DRY_RUN )); then
    "${KUBECTL[@]}" delete job basemap-hpc --ignore-not-found --wait
fi
manifest="$(sed -e "s/__CODE_REV__/$CODE_REV/" \
                -e "s/__RUN_ID__/$RUN_ID/" \
                -e "s/__SHARDS__/$SHARDS/" \
                -e "s/__MAX_ZOOM__/$MAX_ZOOM/" \
                -e "s/__PHASE__/$PHASE/" \
                "$HPC_DIR/50-hpc-driver-job.yaml")"
if (( DRY_RUN )); then
    manifest="$(sed '0,/name: basemap-hpc$/s//name: basemap-hpc-dryrun/' <<<"$manifest")"
fi
"${APPLY[@]}" -f - <<<"$manifest"

(( DRY_RUN )) && { ok "Dry run passed"; exit 0; }
ok "Started as run $RUN_ID. Follow it with:"
echo "  kubectl -n $NAMESPACE logs -f job/basemap-hpc"
echo "When it finishes:"
echo "  RUN_ID=$RUN_ID ./deploy/edito/xyz-publish/run.sh"
