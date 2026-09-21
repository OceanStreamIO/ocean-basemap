#!/usr/bin/env bash
# =============================================================================
# create-distribution.sh — put CloudFront in front of the bucket
# =============================================================================
# The archives are read a few kilobytes at a time: one session is about 450
# requests for 10 MB. The endpoint speaks HTTP/1.1 only, so a browser queues
# those six at a time, and a European reader waits most of a second for a map
# that is milliseconds away in Warsaw. CloudFront answers on HTTP/2 and HTTP/3
# from an edge, which is most of the win; the caching is the smaller half.
#
#   ./create-distribution.sh                # create it, wait, print the domain
#   ./create-distribution.sh --dry-run      # show the config, change nothing
#
# Needs AWS credentials with cloudfront:CreateDistribution. Nothing else in the
# account is touched: the origin is CloudFerro, so there is no bucket, no role
# and no certificate to make unless a custom domain is wanted later.
#
# Notes on the settings, which are the parts worth getting right:
#
#   * The origin keeps the bucket in its hostname, because RGW resolves the
#     bucket from the Host header. CloudFront sends the origin's own domain,
#     so that works without a custom header.
#   * Range requests need no configuration. CloudFront fetches and caches the
#     object in chunks and serves ranges out of them, which is exactly how
#     PMTiles reads.
#   * CORS needs none either, as long as the origin keeps answering
#     "Access-Control-Allow-Origin: *". Because the value is constant, Origin
#     is deliberately NOT in the cache key -- forwarding it would split the
#     cache by requesting site for no benefit.
#   * CachingOptimized honours the origin's Cache-Control within its bounds,
#     so the five-minute archives stay five-minute at the edge too.
#   * PriceClass_100 is North America and Europe. Add the rest when there are
#     readers in it.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/distribution.json"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || err "aws CLI not found"
aws sts get-caller-identity >/dev/null 2>&1 || err "no AWS credentials; configure them first"

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
python3 - "$CONFIG" "$tmp" <<'PY'
import json, sys, time
config = json.load(open(sys.argv[1]))
config["CallerReference"] = f"ocean-basemap-{int(time.time())}"
json.dump(config, open(sys.argv[2], "w"), indent=2)
PY

if [[ "${1:-}" == "--dry-run" ]]; then
    info "Would create this distribution"
    cat "$tmp"
    exit 0
fi

info "Creating the distribution"
id="$(aws cloudfront create-distribution --distribution-config "file://$tmp" \
        --query 'Distribution.Id' --output text)"
domain="$(aws cloudfront get-distribution --id "$id" \
        --query 'Distribution.DomainName' --output text)"
ok "$id  →  https://$domain"

info "Waiting for it to deploy (this takes a few minutes)"
aws cloudfront wait distribution-deployed --id "$id"
ok "deployed"

info "Checking it the way a browser will"
proto="$(curl -s -o /dev/null -w '%{http_version}' "https://$domain/basemap/style.json")"
range="$(curl -s -o /dev/null -D- -r 0-15 "https://$domain/basemap/tiles/bathy_global.pmtiles" \
        | grep -ci '^content-range' || true)"
cors="$(curl -sI "https://$domain/basemap/style.json" | grep -ci 'access-control-allow-origin' || true)"
echo "  HTTP version: $proto   range requests: $range   CORS header: $cors"
[[ "$proto" == "2" || "$proto" == "3" ]] || err "still on HTTP/$proto -- the point was multiplexing"
(( range )) || err "range requests are not being served; PMTiles cannot be read this way"
(( cors ))  || err "the CORS header is not coming through"

cat <<NEXT

Point the map at it:

  BASEMAP_TILE_BASE=https://$domain/basemap ./deploy/edito/apply.sh
  BASEMAP_TILE_BASE=https://$domain/basemap ./deploy/edito/publish-viewer.sh
  python scripts/export-style.py --out dist/style.json   # then publish it

NEXT
