#!/usr/bin/env bash
# =============================================================================
# run.sh — download MERIT Hydro into the build prefix, as a Job
# =============================================================================
#   ./deploy/edito/merit-download/run.sh             # unlock, list, start the Job
#   ./deploy/edito/merit-download/run.sh --dry-run   # unlock and list; validate only
#   MERIT_GRIDS="dir upa wth hnd" ./deploy/edito/merit-download/run.sh
#
# Needs MERIT_HYDRO_PASSWORD in ~/work/.env (from MERIT's registration email)
# and Playwright with Chromium in this pod (see the workspace guide). The
# browser unlocks the share and lists it here; the Job does the downloading.
# The unlocked session is a credential: it lives in a temporary directory that
# this script removes, and in the merit-dropbox-session Secret, which can be
# deleted once the Job is done:
#     kubectl -n project-oceanstream delete secret merit-dropbox-session
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-project-oceanstream}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="${NODE:-$HOME/work/tools/node/bin/node}"
PLAYWRIGHT_MODULE="${PLAYWRIGHT_MODULE:-$HOME/work/workspace/oceanstream_webapp_new/node_modules/playwright}"
export CHROMIUM="${CHROMIUM:-$HOME/work/tools/playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell}"
export MERIT_GRIDS="${MERIT_GRIDS:-dir upa wth}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

info() { printf '\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$NODE" ] || err "node not found at $NODE"
[ -d "$PLAYWRIGHT_MODULE" ] || err "Playwright not found at $PLAYWRIGHT_MODULE"
[ -x "$CHROMIUM" ] || err "Chromium not found at $CHROMIUM"

# The password, without echoing it: sourced into this process only.
if [ -z "${MERIT_HYDRO_PASSWORD:-}" ] && [ -f "$HOME/work/.env" ]; then
    set -a; . "$HOME/work/.env" >/dev/null 2>&1; set +a
fi
[ -n "${MERIT_HYDRO_PASSWORD:-}" ] || err "MERIT_HYDRO_PASSWORD is not set (put it in ~/work/.env)"
export MERIT_HYDRO_PASSWORD

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
chmod 700 "$OUT"

info "Unlocking the share and listing $MERIT_GRIDS"
"$NODE" "$HERE/session.js" "$PLAYWRIGHT_MODULE" "$OUT" || err "could not unlock or list the share"
for grid in $MERIT_GRIDS; do
    n="$(python3 -c 'import json,sys; print(sum(e["name"].startswith(sys.argv[2] + "_") for e in json.load(open(sys.argv[1]))))' \
         "$OUT/manifest.json" "$grid")"
    [ "$n" -gt 0 ] || err "no $grid packages in the listing"
done
ok "manifest and session ready"

KUBECTL=(kubectl -n "$NAMESPACE")
APPLY=("${KUBECTL[@]}" apply)
(( DRY_RUN )) && APPLY+=(--dry-run=server)

info "Secret and ConfigMap"
"${KUBECTL[@]}" create secret generic merit-dropbox-session \
    --from-file=cookies.txt="$OUT/cookies.txt" --dry-run=client -o yaml | "${APPLY[@]}" -f - >/dev/null
"${KUBECTL[@]}" create configmap merit-download \
    --from-file=job.sh="$HERE/job.sh" --from-file=manifest.json="$OUT/manifest.json" \
    --dry-run=client -o yaml | "${APPLY[@]}" -f - >/dev/null
ok "merit-dropbox-session, merit-download"

info "Job"
if (( DRY_RUN )); then
    sed 's/name: merit-download$/name: merit-download-dryrun/' "$HERE/10-merit-download-job.yaml" | "${APPLY[@]}" -f -
    ok "Dry run passed; nothing was started"
    exit 0
fi
"${KUBECTL[@]}" delete job merit-download --ignore-not-found --wait
"${APPLY[@]}" -f "$HERE/10-merit-download-job.yaml"
ok "Started. Follow it with:"
echo "  kubectl -n $NAMESPACE logs -f job/merit-download"
echo "Then stage and trace on the cluster:"
echo "  PHASE=sync,rivers ./deploy/hpc/run.sh"
