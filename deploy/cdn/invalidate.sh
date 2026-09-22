#!/usr/bin/env bash
# =============================================================================
# invalidate.sh — drop the CDN's copy of the few files that change in place
# =============================================================================
# The archives never need this: their names contain a digest of their bytes, so
# a republish is a new URL and the old one is still correct. What is left are
# the handful of files published under fixed names -- the viewer, its config
# and the style -- and for those the CDN has to be told.
#
# It cannot be told with a query string. The CachingOptimized cache policy has
# QueryStrings: none, so `?v=38` is not part of the cache key and CloudFront
# serves whatever it holds for the path regardless. That busts a *browser*
# cache and does nothing at the edge, which is a confusing way to lose an hour:
# the bucket has the new file, the edge serves the old one, and every header
# looks right.
#
#   ./deploy/cdn/invalidate.sh '/basemap/viewer/*' '/basemap/style.json'
#   ./deploy/cdn/invalidate.sh --wait '/basemap/viewer/*'
#
# The distribution comes from $BASEMAP_CF_DISTRIBUTION or the id file beside
# this script. With neither, it says so and exits 0: a deploy to the bucket
# still succeeded, and failing it would be wrong.
#
# CloudFront gives 1,000 invalidation paths a month free. A viewer deploy uses
# one or two, which is why versioned archive names matter -- 20 archives on
# every publish would not fit in that.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ID_FILE="$HERE/distribution-id"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*" >&2; }

WAIT=0
[[ "${1:-}" == "--wait" ]] && { WAIT=1; shift; }
(( $# )) || { warn "no paths given"; exit 0; }

DIST="${BASEMAP_CF_DISTRIBUTION:-}"
[[ -z "$DIST" && -f "$ID_FILE" ]] && DIST="$(tr -d '[:space:]' < "$ID_FILE")"
if [[ -z "$DIST" ]]; then
    warn "no CloudFront distribution configured; skipping invalidation"
    warn "set BASEMAP_CF_DISTRIBUTION or write the id to ${ID_FILE#"$HERE/"}"
    exit 0
fi

# See create-distribution.sh: this pod's CloudFerro S3 environment outranks
# ~/.aws and would send these calls to Warsaw with a Ceph key.
AWS=(env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
         -u AWS_ENDPOINT_URL -u AWS_DEFAULT_REGION -u AWS_REGION aws)
if ! "${AWS[@]}" sts get-caller-identity >/dev/null 2>&1; then
    warn "no AWS credentials; skipping invalidation of $*"
    exit 0
fi

info "Invalidating $# path(s) on $DIST"
id="$("${AWS[@]}" cloudfront create-invalidation --distribution-id "$DIST" \
        --paths "$@" --query 'Invalidation.Id' --output text)"
ok "$id"

if (( WAIT )); then
    "${AWS[@]}" cloudfront wait invalidation-completed \
        --distribution-id "$DIST" --id "$id"
    ok "completed"
fi
