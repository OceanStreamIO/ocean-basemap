#!/usr/bin/env bash
# =============================================================================
# run.sh — publish the traced river network under the ODbL
# =============================================================================
#   ./deploy/edito/rivers-publish/run.sh 2026-10-22
#   ./deploy/edito/rivers-publish/run.sh --dry-run 2026-10-22
#
# The network is a database derived from MERIT Hydro, which we use under the
# ODbL, so it is published under the ODbL too (docs/pipeline/merit-rivers.md).
# This copies it from the private build prefix to a dated public folder, server
# side, with the README beside it:
#
#   s3://oceanstream-bucket/basemap/rivers/merit-rivers-<date>/
#   https://basemap.oceanstream.io/rivers/merit-rivers-<date>/
#
# A dated folder is never overwritten; a new trace gets a new date, and the
# README in this directory is updated for it first (its counts and checks are
# per version). Credentials come from the environment (see ~/work/.env).
# =============================================================================
set -euo pipefail

BUCKET="${BASEMAP_S3_BUCKET:-oceanstream-bucket}"
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"
SRC_KEY="${BASEMAP_RIVERS_KEY:-basemap-build/out/rivers/merit_rivers.fgb}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && { DRY_RUN=1; shift; info "DRY RUN — nothing will be changed"; }
DATE="${1:-}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || err "name the trace's date, e.g. 2026-10-22"
grep -q "— $DATE\$" "$HERE/README.md" || err "README.md is not written for $DATE; update it first"

DEST="basemap/rivers/merit-rivers-$DATE"
AWS=(aws --endpoint-url "$ENDPOINT")
size_of() {
    "${AWS[@]}" s3api head-object --bucket "$BUCKET" --key "$1" \
        --query ContentLength --output text 2>/dev/null || true
}

src_size="$(size_of "$SRC_KEY")"
[ -n "$src_size" ] || err "no network at s3://$BUCKET/$SRC_KEY"
[ -z "$(size_of "$DEST/merit_rivers.fgb")" ] || err "$DEST already exists; a dated folder is never overwritten"
info "s3://$BUCKET/$SRC_KEY ($((src_size / 1000000)) MB) → s3://$BUCKET/$DEST/"
(( DRY_RUN )) && { ok "would copy the network and upload the README"; exit 0; }

# Immutable: the folder is dated and never rewritten.
"${AWS[@]}" s3 cp --only-show-errors "s3://$BUCKET/$SRC_KEY" "s3://$BUCKET/$DEST/merit_rivers.fgb" \
    --metadata-directive REPLACE --content-type application/octet-stream \
    --cache-control "public, max-age=31536000, immutable"
"${AWS[@]}" s3 cp --only-show-errors "$HERE/README.md" "s3://$BUCKET/$DEST/README.md" \
    --content-type "text/markdown; charset=utf-8" --cache-control "public, max-age=3600"
[ "$(size_of "$DEST/merit_rivers.fgb")" = "$src_size" ] || err "the copy is not the source's size"
ok "published: https://basemap.oceanstream.io/rivers/merit-rivers-$DATE/"
