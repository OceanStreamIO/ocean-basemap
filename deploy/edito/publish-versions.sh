#!/usr/bin/env bash
# =============================================================================
# publish-versions.sh — give every published archive an immutable URL
# =============================================================================
# The archives are republished in place under a fixed name. That is convenient
# and it has one sharp edge: a browser holding a PMTiles directory page from
# the old file will read byte ranges out of the new one, and what it draws is
# neither. The symptom is a map with flat, straight-edged holes in it, and it
# survives until the five-minute TTL expires -- or indefinitely in a tab that
# never refetches. It is also why a CDN in front of this has to be invalidated
# on every publish, and why a pinned style does not in fact pin the data.
#
# So each archive is also published as
#
#     basemap/tiles/v/<name>-<version>.pmtiles
#
# where <version> is a digest of the object's own bytes. That name is never
# reused, so it is served with a year's cache-control and "immutable"; it never
# needs invalidating, and anything holding it is pinned to exactly those bytes.
# The unversioned name stays exactly as it was -- a five-minute pointer -- so
# nothing that already refers to it breaks.
#
# basemap/tiles/versions.json maps each name to its current version. The viewer
# deploy reads it and bakes the versioned URLs into config.js, which is how the
# published style comes to carry them too, since the style is exported from the
# running map.
#
#   ./deploy/edito/publish-versions.sh                # publish and write the manifest
#   ./deploy/edito/publish-versions.sh --dry-run      # say what it would do
#   ./deploy/edito/publish-versions.sh --prune 2      # ... and keep only the
#                                                     #     newest 2 old versions
#
# Pruning deletes versioned copies that the manifest no longer names. Anyone
# who pinned one loses it, so it is never the default.
#
# The copy is server side: a 6 GB archive is duplicated in seconds and never
# travels through this pod. Credentials come from the environment (~/work/.env).
# =============================================================================
set -euo pipefail

BUCKET="${BASEMAP_S3_BUCKET:-oceanstream-bucket}"
ENDPOINT="${BASEMAP_S3_ENDPOINT:-https://s3.waw3-1.cloudferro.com}"
PREFIX="${BASEMAP_PUBLIC_PREFIX:-basemap/tiles}"
VERSION_DIR="v"
MANIFEST="versions.json"
CACHE_IMMUTABLE="public, max-age=31536000, immutable"   # S3_CACHE_CONTROL_IMMUTABLE
CACHE_MANIFEST="public, max-age=300"
CONTENT_TYPE="application/vnd.pmtiles"

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
note() { printf '\033[2m    %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

DRY_RUN=0
PRUNE_KEEP=""
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; info "DRY RUN — nothing will be changed"; shift ;;
        --prune)   PRUNE_KEEP="${2:-}"; [[ "$PRUNE_KEEP" =~ ^[0-9]+$ ]] || err "--prune needs a count"; shift 2 ;;
        *) err "unknown argument: $1" ;;
    esac
done

command -v aws >/dev/null || err "aws CLI not found"
AWS=(aws --endpoint-url "$ENDPOINT")
"${AWS[@]}" s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
    || err "cannot reach s3://$BUCKET; source ~/work/.env first"

# --------------------------------------------------------------------------
# What is published, and what each object's bytes hash to
# --------------------------------------------------------------------------
# The digest is taken from the object's ETag and size rather than by reading
# the archive: RGW computes the ETag on upload, it changes whenever the content
# does, and hashing 17 GB through this pod to learn the same thing would take
# minutes and cost egress. It is a cache key, not a checksum anyone verifies.
info "Reading $PREFIX"
listing="$("${AWS[@]}" s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX/" \
    --query 'Contents[].{Key:Key,ETag:ETag,Size:Size,Modified:LastModified}' --output json)"

plan="$(VERSION_DIR="$VERSION_DIR" PREFIX="$PREFIX" python3 - "$listing" <<'PY'
import hashlib, json, os, posixpath, sys

listing = json.loads(sys.argv[1] or "[]") or []
prefix, version_dir = os.environ["PREFIX"], os.environ["VERSION_DIR"]

current, existing = {}, set()
for obj in listing:
    rel = posixpath.relpath(obj["Key"], prefix)
    if not rel.endswith(".pmtiles"):
        continue
    if rel.startswith(f"{version_dir}/"):
        existing.add(rel)
        continue
    if "/" in rel:
        continue  # some other subdirectory; not ours to version
    digest = hashlib.sha256(
        f"{obj['ETag'].strip(chr(34))}:{obj['Size']}".encode()
    ).hexdigest()[:12]
    name = rel[: -len(".pmtiles")]
    current[name] = {
        "version": digest,
        "size": obj["Size"],
        "modified": obj["Modified"],
        "path": f"{version_dir}/{name}-{digest}.pmtiles",
    }

print(json.dumps({
    "current": current,
    "existing": sorted(existing),
    "todo": sorted(v["path"] for v in current.values() if v["path"] not in existing),
}))
PY
)"

count="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["current"]))' "$plan")"
todo="$(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])["todo"]))' "$plan")"
(( count )) || err "no archives found under s3://$BUCKET/$PREFIX/"
ok "$count archives published"

# --------------------------------------------------------------------------
# Copy the ones that do not have an immutable name yet
# --------------------------------------------------------------------------
if [[ -z "$todo" ]]; then
    ok "every archive already has its immutable copy"
else
    info "Publishing immutable copies"
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        name="${path#"$VERSION_DIR"/}"; name="${name%-*}"
        if (( DRY_RUN )); then
            note "would copy $name.pmtiles → $path"
            continue
        fi
        "${AWS[@]}" s3 cp "s3://$BUCKET/$PREFIX/$name.pmtiles" "s3://$BUCKET/$PREFIX/$path" \
            --content-type "$CONTENT_TYPE" --cache-control "$CACHE_IMMUTABLE" \
            --metadata-directive REPLACE --only-show-errors
        ok "$path"
    done <<< "$todo"
fi

# --------------------------------------------------------------------------
# The manifest the viewer deploy reads
# --------------------------------------------------------------------------
info "Manifest"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
python3 - "$plan" "$VERSION_DIR" > "$tmp" <<'PY'
import json, sys, time
plan = json.loads(sys.argv[1])
print(json.dumps({
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "dir": sys.argv[2],
    "archives": {
        name: {"version": v["version"], "size": v["size"], "path": v["path"]}
        for name, v in sorted(plan["current"].items())
    },
}, indent=2))
PY
if (( DRY_RUN )); then
    note "would publish $MANIFEST:"
    sed 's/^/    /' "$tmp"
else
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
    "${AWS[@]}" s3 cp "$tmp" "s3://$BUCKET/$PREFIX/$MANIFEST" \
        --content-type application/json --cache-control "$CACHE_MANIFEST" --only-show-errors
    ok "$PREFIX/$MANIFEST"
fi

# --------------------------------------------------------------------------
# Optional pruning
# --------------------------------------------------------------------------
if [[ -n "$PRUNE_KEEP" ]]; then
    info "Pruning to the newest $PRUNE_KEEP superseded versions per archive"
    stale="$(KEEP="$PRUNE_KEEP" VERSION_DIR="$VERSION_DIR" PREFIX="$PREFIX" \
        python3 - "$listing" "$plan" <<'PY'
import json, os, posixpath, sys
from collections import defaultdict

listing = json.loads(sys.argv[1] or "[]") or []
plan = json.loads(sys.argv[2])
keep, prefix, version_dir = int(os.environ["KEEP"]), os.environ["PREFIX"], os.environ["VERSION_DIR"]
live = {v["path"] for v in plan["current"].values()}

by_name = defaultdict(list)
for obj in listing:
    rel = posixpath.relpath(obj["Key"], prefix)
    if not rel.startswith(f"{version_dir}/") or not rel.endswith(".pmtiles") or rel in live:
        continue
    name = rel[len(version_dir) + 1 : -len(".pmtiles")].rsplit("-", 1)[0]
    by_name[name].append((obj["Modified"], rel))

for name, rows in by_name.items():
    for _, rel in sorted(rows, reverse=True)[keep:]:
        print(rel)
PY
)"
    if [[ -z "$stale" ]]; then
        ok "nothing to prune"
    else
        while IFS= read -r rel; do
            [[ -n "$rel" ]] || continue
            if (( DRY_RUN )); then
                note "would delete $rel"
            else
                "${AWS[@]}" s3 rm "s3://$BUCKET/$PREFIX/$rel" --only-show-errors
                ok "deleted $rel"
            fi
        done <<< "$stale"
    fi
fi

echo
echo "Now redeploy the viewer so it points at these versions:"
echo "  ./deploy/edito/apply.sh && ./deploy/edito/publish-viewer.sh"
