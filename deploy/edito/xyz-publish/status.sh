#!/usr/bin/env bash
# =============================================================================
# How far the publish has got
# =============================================================================
# Reads the marks volume rather than counting objects: listing twenty-two
# million keys takes longer than sending them.
# =============================================================================
set -euo pipefail
NAMESPACE="${NAMESPACE:-project-oceanstream}"
K=(kubectl -n "$NAMESPACE")

POD="$("${K[@]}" get pods -l app=basemap-xyz-publish \
       -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' 2>/dev/null | awk '{print $1}')"

printf '\033[1;34m▶ Job\033[0m\n'
"${K[@]}" get job basemap-xyz-publish 2>/dev/null || echo "  (no publish job)"
"${K[@]}" get pods -l app=basemap-xyz-publish \
    -o custom-columns='POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName' 2>/dev/null | head -5

if [ -z "$POD" ]; then
    printf '\033[1;34m▶ Recent output\033[0m\n'
    "${K[@]}" logs job/basemap-xyz-publish --tail=20 2>/dev/null \
        | grep -E '^(===|###|  [0-9])' | tail -12 || echo "  (no logs)"
    exit 0
fi

printf '\033[1;34m▶ Progress\033[0m\n'
"${K[@]}" exec "$POD" -- python3 - <<'PY' 2>/dev/null || echo "  (marks not readable yet)"
import sqlite3
try:
    db = sqlite3.connect("file:/marks/xyz.sent.sqlite?mode=ro", uri=True)
    done = db.execute("SELECT count(*) FROM sent").fetchone()[0]
    by_zoom = dict(db.execute("SELECT z, count(*) FROM sent GROUP BY z"))
except sqlite3.OperationalError as exc:
    raise SystemExit(f"  marks unavailable: {exc}")

total = sum(1 << z for z in range(13))
print(f"  {done:,} of {total:,} columns sent ({100 * done / total:.1f}%)")
for z in range(13):
    have, want = by_zoom.get(z, 0), 1 << z
    if have:
        mark = "done" if have == want else f"{100 * have / want:.0f}%"
        print(f"    z{z:<3} {have:>5,} / {want:<5,}  {mark}")
PY

printf '\033[1;34m▶ Rate\033[0m\n'
"${K[@]}" logs "$POD" --tail=200 2>/dev/null | grep -oE '[0-9,]+ sent, [0-9,]+/s' | tail -3 \
    | sed 's/^/  /' || echo "  (no rate reported yet)"
"${K[@]}" top pod "$POD" 2>/dev/null | tail -1 | sed 's/^/  /' || true
