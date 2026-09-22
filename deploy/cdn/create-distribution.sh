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
#   * CORS is added at the edge, by a response headers policy, and this is not
#     optional. RGW answers Access-Control-Allow-Origin only when the request
#     carries an Origin header, and Origin is deliberately NOT in the cache key
#     -- forwarding it would split the cache by requesting site for no benefit,
#     since the answer is "*" for everyone. With it neither forwarded nor
#     added, no cached response carries CORS at all and every browser refuses
#     the archive. The policy also has to expose Content-Range: without it the
#     browser hides the header from JavaScript and pmtiles.js cannot read an
#     archive, failing silently as an empty map.
#   * CachingOptimized honours the origin's Cache-Control within its bounds,
#     so the five-minute archives stay five-minute at the edge too.
#   * PriceClass_100 is North America and Europe. Add the rest when there are
#     readers in it.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/distribution.json"
CORS_POLICY="$HERE/response-headers-policy.json"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || err "aws CLI not found"

# This pod is wired to CloudFerro's S3, not to AWS: AWS_ACCESS_KEY_ID,
# AWS_ENDPOINT_URL and AWS_DEFAULT_REGION are exported into every shell and
# they outrank ~/.aws/credentials. Left alone, every call below would be sent
# to s3.waw3-1.cloudferro.com with a Ceph key and fail as "Unknown". So the
# AWS-proper calls run with that environment stripped, and only those: the
# S3 work elsewhere in deploy/ still wants it.
AWS=(env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
         -u AWS_ENDPOINT_URL -u AWS_DEFAULT_REGION -u AWS_REGION aws)

"${AWS[@]}" sts get-caller-identity >/dev/null 2>&1 \
    || err "no AWS credentials for CloudFront; configure them in ~/.aws/credentials"

# The response headers policy is shared by every distribution we make and is
# named, not anonymous, so a second run reuses it instead of piling up copies.
policy_name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Name"])' "$CORS_POLICY")"
policy_id="$("${AWS[@]}" cloudfront list-response-headers-policies \
    --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='$policy_name'].ResponseHeadersPolicy.Id | [0]" \
    --output text 2>/dev/null || true)"
if [[ -z "$policy_id" || "$policy_id" == "None" ]]; then
    info "Creating the response headers policy $policy_name"
    policy_id="$("${AWS[@]}" cloudfront create-response-headers-policy \
        --response-headers-policy-config "file://$CORS_POLICY" \
        --query 'ResponseHeadersPolicy.Id' --output text)"
fi
ok "CORS policy $policy_id"

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
POLICY_ID="$policy_id" python3 - "$CONFIG" "$tmp" <<'PY'
import json, os, sys, time
config = json.load(open(sys.argv[1]))
config["CallerReference"] = f"ocean-basemap-{int(time.time())}"
config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"] = os.environ["POLICY_ID"]
json.dump(config, open(sys.argv[2], "w"), indent=2)
PY

if [[ "${1:-}" == "--dry-run" ]]; then
    info "Would create this distribution"
    cat "$tmp"
    exit 0
fi

info "Creating the distribution"
id="$("${AWS[@]}" cloudfront create-distribution --distribution-config "file://$tmp" \
        --query 'Distribution.Id' --output text)"
domain="$("${AWS[@]}" cloudfront get-distribution --id "$id" \
        --query 'Distribution.DomainName' --output text)"
ok "$id  →  https://$domain"
# invalidate.sh reads this; the id is not a secret, the domain is public.
printf '%s\n' "$id" > "$HERE/distribution-id"

info "Waiting for it to deploy (this takes a few minutes)"
"${AWS[@]}" cloudfront wait distribution-deployed --id "$id"
ok "deployed"

info "Checking it the way a browser will"
proto="$(curl -s -o /dev/null -w '%{http_version}' "https://$domain/basemap/style.json")"
range="$(curl -s -o /dev/null -D- -r 0-15 "https://$domain/basemap/tiles/bathy_global.pmtiles" \
        | grep -ci '^content-range' || true)"
# As a browser asks: with an Origin header, and checking that Content-Range is
# exposed, which is what pmtiles.js actually needs and what fails silently.
headers="$(curl -sI -H 'Origin: https://example.org' "https://$domain/basemap/style.json")"
cors="$(grep -ci 'access-control-allow-origin' <<< "$headers" || true)"
expose="$(grep -i 'access-control-expose-headers' <<< "$headers" | grep -ci 'content-range' || true)"
echo "  HTTP version: $proto   range: $range   CORS: $cors   Content-Range exposed: $expose"
[[ "$proto" == "2" || "$proto" == "3" ]] || err "still on HTTP/$proto -- the point was multiplexing"
(( range )) || err "range requests are not being served; PMTiles cannot be read this way"
(( cors ))  || err "the CORS header is not coming through"
(( expose )) || err "Content-Range is not exposed; pmtiles.js would read an empty archive"

cat <<NEXT

Point the map at it:

  BASEMAP_TILE_BASE=https://$domain/basemap ./deploy/edito/apply.sh
  BASEMAP_TILE_BASE=https://$domain/basemap ./deploy/edito/publish-viewer.sh
  python scripts/export-style.py --out dist/style.json   # then publish it

NEXT
