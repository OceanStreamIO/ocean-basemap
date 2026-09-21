#!/usr/bin/env bash
# =============================================================================
# promote.sh — publish an archive the build Job produced
# =============================================================================
# The Job writes to the private basemap-build/out/ prefix, never to the public
# one, so that a finished build can be checked before anyone sees it. This is
# the step that makes it public.
#
#   ./deploy/edito/relief-build/promote.sh land_relief
#   ./deploy/edito/relief-build/promote.sh --dry-run land_relief land_relief_eu
#
# The copy is server side: the object never travels through this pod, so a
# 3 GB archive is promoted in seconds. Content type and cache control have to
# be restated, because S3 drops the source metadata unless told to replace it,
# and a PMTiles archive served as application/octet-stream still works while a
# wrong cache-control quietly pins a stale build in every browser.
#
# Credentials come from the environment (see ~/work/.env).
# =============================================================================
set -euo pipefail

BUCKET="${BASEMAP_S3_BUCKET:-oceanstream-bucket}"
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"
SRC_PREFIX="${BASEMAP_BUILD_PREFIX:-basemap-build/out/tiles}"
DST_PREFIX="${BASEMAP_PUBLIC_PREFIX:-basemap/tiles}"
CONTENT_TYPE="application/vnd.pmtiles"
CACHE_CONTROL="public, max-age=300"   # see S3_CACHE_CONTROL_PMTILES in config.py

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && { DRY_RUN=1; shift; info "DRY RUN — nothing will be changed"; }
(( $# )) || err "name at least one archive, e.g. land_relief"

AWS=(aws --endpoint-url "$ENDPOINT")

size_of() {  # bytes of an object, or empty when it is not there
    "${AWS[@]}" s3api head-object --bucket "$BUCKET" --key "$1" \
        --query ContentLength --output text 2>/dev/null || true
}

for name in "$@"; do
    src="$SRC_PREFIX/$name.pmtiles"
    dst="$DST_PREFIX/$name.pmtiles"
    info "$name"

    new="$(size_of "$src")"
    [[ -n "$new" && "$new" != "None" ]] || err "$src is not in the build output"
    old="$(size_of "$dst")"
    printf '  build output : %'"'"'d bytes\n' "$new"
    if [[ -n "$old" && "$old" != "None" ]]; then
        printf '  live now     : %'"'"'d bytes (%+d)\n' "$old" $((new - old))
    else
        echo "  live now     : nothing published yet"
    fi

    if (( DRY_RUN )); then
        ok "would copy s3://$BUCKET/$src → s3://$BUCKET/$dst"
        continue
    fi
    "${AWS[@]}" s3 cp "s3://$BUCKET/$src" "s3://$BUCKET/$dst" \
        --content-type "$CONTENT_TYPE" \
        --cache-control "$CACHE_CONTROL" \
        --metadata-directive REPLACE \
        --only-show-errors
    live="$(size_of "$dst")"
    [[ "$live" == "$new" ]] || err "published size $live does not match the build's $new"
    ok "published, $live bytes"
done

echo
echo "Check one before telling anyone:"
echo "  curl -sI -r 0-16 https://$BUCKET.s3.${ENDPOINT#https://s3.}/$DST_PREFIX/$1.pmtiles"
