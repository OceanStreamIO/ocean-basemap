#!/usr/bin/env bash
# =============================================================================
# lake-depths.sh — the corrected lake depths that go into the viewer's config.js
# =============================================================================
# Sourced by apply.sh and publish-viewer.sh, which both generate config.js and
# must generate the same one. It lived in publish-viewer.sh alone, so the copy
# on the bucket carried the corrections and the copy in the cluster did not --
# the same lake came out at two different depths depending on which viewer you
# opened.
#
# The table is read from lake-depths.json, the file the renderer itself reads,
# so the vector map and the flat tile tree cannot disagree about a lake.
# =============================================================================

# lake_depths_js
#
# Prints the JavaScript to append to config.js. Prints nothing at all when the
# file is absent, which leaves the viewer on the HydroLAKES values -- the same
# fallback lake_depth_overrides() takes -- and says so on stderr, because a
# silently uncorrected lake is indistinguishable from a correct one.
lake_depths_js() {
    local repo depths
    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    depths="$repo/lake-depths.json"
    if [[ ! -f "$depths" ]]; then
        printf '\033[1;33m  ! no lake-depths.json — lakes keep their HydroLAKES depths\033[0m\n' >&2
        echo "// No lake-depths.json was found; lakes keep their HydroLAKES depths."
        return 0
    fi
    python3 - "$depths" <<'PY'
import json, sys

entries = json.load(open(sys.argv[1])).get("lakes", [])
table = {e["name"]: e["depth_avg_m"] for e in entries if e.get("name")}
print(f"// {len(table)} corrected lake depths, from lake-depths.json")
print(f"window.BASEMAP_LAKE_DEPTHS = {json.dumps(table)};")
PY
}
