#!/usr/bin/env bash
# =============================================================================
# job.sh — stream MERIT Hydro's packages from Dropbox into the build prefix
# =============================================================================
# Runs inside the merit-download Job. The manifest (name, bytes, link) and the
# unlocked Dropbox session come from run.sh, as a ConfigMap and a Secret.
# Nothing lands on disk: each package goes curl -> aws s3 cp -, and is checked
# against the listed size afterwards. A package already in the bucket at its
# listed size is skipped, so a rerun resumes.
#
# The destination is private on purpose: MERIT asks users not to redistribute
# the data "in whole and in its original format".
# =============================================================================
set -euo pipefail

DEST="${MERIT_S3_PREFIX:-s3://oceanstream-bucket/basemap-build/merit_hydro}"
PARALLEL="${MERIT_PARALLEL:-4}"
MANIFEST=/opt/merit/manifest.json
COOKIES=/etc/merit-session/cookies.txt
export HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools UV_TOOL_BIN_DIR=/tmp/uv-bin
export PATH="/tmp/uv-bin:$PATH" AWS_EC2_METADATA_DISABLED=true

echo "=== tools ==="
apt-get -qq update >/dev/null 2>&1
apt-get -qq install -y --no-install-recommends curl ca-certificates >/dev/null 2>&1
uv tool install --quiet awscli
echo "  $(curl --version | head -1 | cut -d' ' -f1-2) · $(aws --version 2>&1 | cut -d' ' -f1)"

fetch() {
    local name="$1" bytes="$2" href="$3"
    local bucket="${DEST#s3://}"; bucket="${bucket%%/*}"
    local key="${DEST#s3://$bucket/}/$name"
    local have
    have="$(aws --endpoint-url "$S3_ENDPOINT" s3api head-object --bucket "$bucket" --key "$key" \
            --query ContentLength --output text 2>/dev/null || true)"
    if [ "$have" = "$bytes" ]; then
        echo "  skip $name (already there)"
        return 0
    fi
    # The share link with dl=1 redirects to a short-lived file URL; the cookies
    # only ever go to dropbox.com.
    if curl -fsSL --retry 5 --retry-delay 15 --connect-timeout 30 -b "$COOKIES" \
            -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36' \
            "${href/dl=0/dl=1}" \
        | aws --endpoint-url "$S3_ENDPOINT" s3 cp --only-show-errors --expected-size "$bytes" - "$DEST/$name"; then
        have="$(aws --endpoint-url "$S3_ENDPOINT" s3api head-object --bucket "$bucket" --key "$key" \
                --query ContentLength --output text 2>/dev/null || true)"
        if [ "$have" = "$bytes" ]; then
            echo "  ok   $name $((bytes / 1000000)) MB"
            return 0
        fi
        echo "  FAIL $name: $have of $bytes bytes" >&2
        return 1
    fi
    echo "  FAIL $name: download failed" >&2
    return 1
}
export -f fetch
export DEST COOKIES

echo "=== packages → $DEST ($PARALLEL at a time) ==="
total="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$MANIFEST")"
started=$(date +%s)
set +e
python3 -c 'import json,sys
for e in json.load(open(sys.argv[1])): print(e["name"], e["bytes"], e["href"])' "$MANIFEST" \
    | xargs -P "$PARALLEL" -L 1 bash -c 'fetch "$@"' _
status=$?
set -e
echo "=== $total packages, $(( ($(date +%s) - started) / 60 )) min, exit $status ==="
exit "$status"
