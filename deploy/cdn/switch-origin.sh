#!/usr/bin/env bash
# =============================================================================
# switch-origin.sh — point the published map at a different base URL
# =============================================================================
# Everything that names where the tiles live is generated, so moving the map
# behind a CDN (or back off it) is this one script rather than an edit:
#
#   ./deploy/cdn/switch-origin.sh https://dxxxxx.cloudfront.net/basemap
#   ./deploy/cdn/switch-origin.sh --back          # to the bucket
#
# It redeploys the viewer on EDITO and republishes the static viewer on the
# bucket. That second step also re-exports the style from the map it has just
# published and publishes it -- which is how style.json learns the new URLs,
# since it is read from the map rather than written -- and refuses a style
# that does not point at the new base. First it checks a tile actually comes
# back through the new base, because a CDN that is not yet deployed answers
# 403 and the map would simply be blank.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUCKET_BASE="https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $# -eq 1 ]] || err "usage: switch-origin.sh <base-url> | --back"
BASE="$1"
[[ "$BASE" == "--back" ]] && BASE="$BUCKET_BASE"
BASE="${BASE%/}"

info "Checking $BASE serves the archives"
code="$(curl -s -o /dev/null -r 0-15 -w '%{http_code}' "$BASE/tiles/bathy_global.pmtiles")"
[[ "$code" == "206" ]] || err "a range request returned $code, not 206 -- not ready"
ok "range request answered"

# What apply.sh and publish-viewer.sh will read from now on, so a later deploy
# without BASEMAP_TILE_BASE stays where this run put it.
printf '%s\n' "$BASE" > "$REPO/deploy/cdn/origin"
ok "recorded as the published origin"

info "Viewer on EDITO"
BASEMAP_TILE_BASE="$BASE" "$REPO/deploy/edito/apply.sh" >/dev/null
ok "redeployed"

info "Static viewer on the bucket, and the style read back out of it"
# publish-viewer.sh exports the style from the copy it has just published,
# verifies every archive it names is on the server, insists it points at the
# new base, and publishes style.json and style/v1.json. It exits non-zero if
# any of that fails, which fails this script too.
BASEMAP_TILE_BASE="$BASE" "$REPO/deploy/edito/publish-viewer.sh" >/dev/null
ok "republished, style included"

echo
echo "The map now reads from $BASE"
