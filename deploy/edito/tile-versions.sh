#!/usr/bin/env bash
# =============================================================================
# tile-versions.sh — the versions block that goes into the viewer's config.js
# =============================================================================
# Sourced by apply.sh and publish-viewer.sh, which both generate config.js and
# must generate the same one.
#
# publish-versions.sh writes basemap/tiles/versions.json, mapping each archive
# to a name containing a digest of its bytes. Baking that map into config.js at
# deploy time is what makes the viewer -- and therefore the style exported from
# it -- read immutable URLs instead of names that change underneath them.
#
# Doing it at deploy time rather than fetching the manifest in the browser is
# deliberate: the style is built synchronously as the page loads, an extra
# round trip before the first tile would be the wrong trade for a file that
# changes a few times a week, and pinning the viewer to the archives it was
# deployed with is the property we actually want.
# =============================================================================

# tile_versions_js <pmtiles-base-url>
#
# Prints the JavaScript to append to config.js. Falls back to a comment and no
# assignment when the manifest cannot be read, which leaves the viewer on the
# unversioned names -- working, but back to reading archives that change in
# place. That is worth a warning, so it prints one on stderr.
tile_versions_js() {
    local base="${1%/}" manifest
    manifest="$(curl -fsS --max-time 20 "$base/versions.json" 2>/dev/null || true)"
    if [[ -z "$manifest" ]]; then
        printf '\033[1;33m  ! no versions.json at %s — the viewer will use unversioned archive names\033[0m\n' \
            "$base" >&2
        echo "// No versions.json was published; falling back to unversioned names."
        return 0
    fi
    MANIFEST="$manifest" python3 <<'PY' || {
import json, os, sys

try:
    data = json.loads(os.environ["MANIFEST"])
    archives = data["archives"]
    if not archives:
        raise ValueError("no archives in the manifest")
except Exception as exc:                      # noqa: BLE001 - reported, not raised
    print(f"  ! versions.json is unusable ({exc})", file=sys.stderr)
    raise SystemExit(1)

versions = {name: entry["path"] for name, entry in sorted(archives.items())}
print(f"// {len(versions)} archives, pinned from versions.json generated {data.get('generated', '?')}")
print(f"window.BASEMAP_TILE_VERSIONS = {json.dumps(versions, indent=2)};")
PY
        printf '\033[1;33m  ! versions.json could not be parsed — using unversioned names\033[0m\n' >&2
        echo "// versions.json could not be parsed; falling back to unversioned names."
    }
}
