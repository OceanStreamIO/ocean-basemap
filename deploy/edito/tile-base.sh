#!/usr/bin/env bash
# =============================================================================
# tile-base.sh — where the published viewers read their tiles from
# =============================================================================
# Sourced by apply.sh and publish-viewer.sh, which each generate a config.js
# and must agree on this.
#
# It used to be a default written into both scripts, pointing at the bucket,
# with the CDN supplied by hand as BASEMAP_TILE_BASE on every invocation. That
# works exactly until someone runs a plain ./apply.sh, which then moves the map
# back onto CloudFerro without saying anything: no error, no warning, and
# nothing in the page to show for it but a slower load. The origin belongs in a
# file that the deploy reads, not in a habit.
#
# Order of precedence:
#   1. $BASEMAP_TILE_BASE          -- a one-off, for testing a staged build
#   2. deploy/cdn/origin           -- what switch-origin.sh last published to
#   3. the bucket                  -- correct, and what we fall back to if the
#                                     distribution is ever torn down
# =============================================================================

BASEMAP_BUCKET_BASE="https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap"

# resolve_tile_base — prints the base URL the viewer should be built with.
resolve_tile_base() {
    if [[ -n "${BASEMAP_TILE_BASE:-}" ]]; then
        printf '%s\n' "${BASEMAP_TILE_BASE%/}"
        return 0
    fi
    local here origin
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    origin="$here/../cdn/origin"
    if [[ -s "$origin" ]]; then
        printf '%s\n' "$(tr -d '[:space:]' < "$origin")"
        return 0
    fi
    printf '%s\n' "$BASEMAP_BUCKET_BASE"
}
