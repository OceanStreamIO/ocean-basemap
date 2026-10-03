#!/usr/bin/env bash
# =============================================================================
# run.sh — (re)start the raster relief build as a Job on a GPU node
# =============================================================================
# The Jupyter pod's node has 8 CPUs; a GPU node has 16, and the Job asks for no
# GPU, so it costs no GPU quota. The Job reads its inputs from the private
# s3://oceanstream-bucket/basemap-build/inputs/ prefix onto its own volume and
# writes finished archives to basemap-build/out/ -- never to the public
# basemap/ prefix, which is a separate, checked promotion.
#
#   ./deploy/edito/relief-build/run.sh              # build the current commit
#   ./deploy/edito/relief-build/run.sh --dry-run    # server-side validation only
#   CODE_REV=f4baf8c ./deploy/edito/relief-build/run.sh
#   REBUILD="bathy_global bathy_eu" ./deploy/edito/relief-build/run.sh
#
# XYZ_VERSION publishes the flat tree to basemap/<version>/xyz instead of the
# live prefix, so the switch is one CDN deployment rather than a day of mixed
# tiles. Versions are few and pinnable -- a fix republishes the same one.
#
# REBUILD names archives to clear and render again; everything else already on
# the volume is kept. XYZ_REDO_FROM clears the z/x/y step's resume marks at and
# below a zoom, for when the reason tiles were missing has since been fixed.
#
# Rerunning resumes: the volume keeps downloads and staging databases.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$BUILD_DIR/../../.." && pwd)"
CODE_REV="${CODE_REV:-$(git -C "$REPO" rev-parse --short HEAD)}"
BUCKET=s3://oceanstream-bucket/basemap-build
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || err "kubectl not found"
command -v aws >/dev/null || err "aws CLI not found"
git -C "$REPO" diff --quiet HEAD -- src || info "note: src/ has uncommitted changes; the Job runs $CODE_REV as committed"

KUBECTL=(kubectl -n "$NAMESPACE")
APPLY=("${KUBECTL[@]}" apply)
(( DRY_RUN )) && APPLY+=(--dry-run=server) && info "DRY RUN — nothing will be changed"

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
"${APPLY[@]}" -f "$BUILD_DIR/30-build-pvc.yaml"
"${KUBECTL[@]}" create configmap basemap-build-script --from-file="$BUILD_DIR/job.sh" \
    --dry-run=client -o yaml | "${APPLY[@]}" -f -

info "Job"
if (( ! DRY_RUN )); then
    # A Job's pod template is immutable, so a new run replaces the old one.
    "${KUBECTL[@]}" delete job basemap-build --ignore-not-found --wait
fi
manifest="$(sed -e "s/__CODE_REV__/$CODE_REV/" -e "s/__REBUILD__/${REBUILD:-}/" -e "s/__XYZ_REDO_FROM__/${XYZ_REDO_FROM:-}/" -e "s|__XYZ_VERSION__|${XYZ_VERSION:-}|" -e "s|__STEPS__|${BUILD_STEPS:-}|" "$BUILD_DIR/31-build-job.yaml")"
if (( DRY_RUN )); then
    # Validate under another name: a dry run against the live Job would try to
    # change its immutable pod template and fail for that reason alone.
    manifest="$(sed '0,/name: basemap-build$/s//name: basemap-build-dryrun/' <<<"$manifest")"
fi
"${APPLY[@]}" -f - <<<"$manifest"

(( DRY_RUN )) && { ok "Dry run passed"; exit 0; }
ok "Started. Follow it with:"
echo "  kubectl -n $NAMESPACE logs -f job/basemap-build"
