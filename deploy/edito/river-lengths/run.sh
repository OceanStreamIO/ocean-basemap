#!/usr/bin/env bash
# =============================================================================
# run.sh — measure both river networks as a Job, for the zoom-rule fit
# =============================================================================
#   ./deploy/edito/river-lengths/run.sh             # ship HEAD, start the Job
#   ./deploy/edito/river-lengths/run.sh --dry-run   # validate only
#
# Runs the committed HEAD (git archive), so commit first. Results land in
# s3://oceanstream-bucket/basemap-build/out/rivers/lengths/, and the Job's log
# ends with the fitted rule per zoom.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
CODE_REV="${CODE_REV:-$(git -C "$REPO" rev-parse --short HEAD)}"
BUCKET=s3://oceanstream-bucket/basemap-build
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }

KUBECTL=(kubectl -n "$NAMESPACE")
APPLY=("${KUBECTL[@]}" apply)
(( DRY_RUN )) && APPLY+=(--dry-run=server)

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
"${KUBECTL[@]}" create configmap river-lengths --from-file=job.sh="$HERE/job.sh" \
    --dry-run=client -o yaml | "${APPLY[@]}" -f - >/dev/null
ok "river-lengths"

info "Job"
manifest="$(sed "s/__CODE_REV__/$CODE_REV/" "$HERE/10-river-lengths-job.yaml")"
if (( DRY_RUN )); then
    sed '0,/name: river-lengths$/s//name: river-lengths-dryrun/' <<<"$manifest" | "${APPLY[@]}" -f -
    ok "Dry run passed; nothing was started"
    exit 0
fi
"${KUBECTL[@]}" delete job river-lengths --ignore-not-found --wait
"${APPLY[@]}" -f - <<<"$manifest"
ok "Started. Follow it with:"
echo "  kubectl -n $NAMESPACE logs -f job/river-lengths"
