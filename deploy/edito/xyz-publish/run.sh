#!/usr/bin/env bash
# =============================================================================
# run.sh — (re)start the flat-tile publish as a Job
# =============================================================================
# Publishes the shards a cluster render left in object storage. Ordinary nodes:
# eight sending processes need about five and a half cores, which fits one.
#
#   RUN_ID=20260928T120000Z ./deploy/edito/xyz-publish/run.sh
#   RUN_ID=... XYZ_VERSION=v3 ./deploy/edito/xyz-publish/run.sh
#   RUN_ID=... ./deploy/edito/xyz-publish/run.sh --dry-run
#
# XYZ_VERSION publishes to basemap/<version>/xyz instead of the live prefix, so
# going live is one CDN deployment rather than a day of half-updated tiles.
# Unset, it writes to the live tree, which is what a local run has always done.
#
# PODS fans the work out by completion index. It ships at one: the measured rate
# from one pod is about 1,930 objects a second, and whether the endpoint wants
# more than that is a question to answer by measuring, not by assuming.
#
# Rerunning resumes: finished shards carry a marker and the marks volume keeps
# the per-column record.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
PUBLISH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$PUBLISH_DIR/../../.." && pwd)"
CODE_REV="${CODE_REV:-$(git -C "$REPO" rev-parse --short HEAD)}"
PODS="${PODS:-1}"
BUCKET=s3://oceanstream-bucket/basemap-build
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

: "${RUN_ID:?RUN_ID must name the render to publish (see deploy/hpc/export-tiles.sh)}"
command -v kubectl >/dev/null || err "kubectl not found"
command -v aws >/dev/null || err "aws CLI not found"

KUBECTL=(kubectl -n "$NAMESPACE")
APPLY=("${KUBECTL[@]}" apply)
(( DRY_RUN )) && APPLY+=(--dry-run=server) && info "DRY RUN — nothing will be changed"

info "Run $RUN_ID"
# Fail here rather than three minutes into a pod: the manifest is written last
# by the export, so its absence means the render is not ready to publish.
if aws --endpoint-url "$ENDPOINT" s3 ls "$BUCKET/xyz-shards/$RUN_ID/manifest.json" >/dev/null 2>&1; then
    shards=$(aws --endpoint-url "$ENDPOINT" s3 ls "$BUCKET/xyz-shards/$RUN_ID/" | grep -c 'mbtiles$' || true)
    ok "manifest present, $shards shard file(s)"
else
    err "No manifest at $BUCKET/xyz-shards/$RUN_ID/manifest.json
  Export the render first: ./deploy/hpc/export-tiles.sh --run $RUN_ID"
fi

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

info "Volume and script"
"${APPLY[@]}" -f "$PUBLISH_DIR/40-publish-marks-pvc.yaml"
"${KUBECTL[@]}" create configmap basemap-xyz-publish-script \
    --from-file="$PUBLISH_DIR/job.sh" --dry-run=client -o yaml | "${APPLY[@]}" -f -

info "Job"
if (( ! DRY_RUN )); then
    # A Job's pod template is immutable, so a new run replaces the old one.
    "${KUBECTL[@]}" delete job basemap-xyz-publish --ignore-not-found --wait
fi
manifest="$(sed -e "s/__CODE_REV__/$CODE_REV/" \
                -e "s/__RUN_ID__/$RUN_ID/" \
                -e "s/__PODS__/$PODS/" \
                -e "s|__XYZ_VERSION__|${XYZ_VERSION:-}|" \
                "$PUBLISH_DIR/41-xyz-publish-job.yaml")"
if (( DRY_RUN )); then
    # Validated under another name: a dry run against the live Job would try to
    # change its immutable pod template and fail for that reason alone.
    manifest="$(sed '0,/name: basemap-xyz-publish$/s//name: basemap-xyz-publish-dryrun/' <<<"$manifest")"
fi
"${APPLY[@]}" -f - <<<"$manifest"

(( DRY_RUN )) && { ok "Dry run passed"; exit 0; }
ok "Started. Follow it with:"
echo "  kubectl -n $NAMESPACE logs -f job/basemap-xyz-publish"
echo "  ./deploy/edito/xyz-publish/status.sh"
