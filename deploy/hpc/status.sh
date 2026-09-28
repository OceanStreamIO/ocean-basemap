#!/usr/bin/env bash
# =============================================================================
# What the build is doing, across both clusters
# =============================================================================
#   ./deploy/hpc/status.sh              # one look
#   ./deploy/hpc/status.sh --watch      # every 30s until nothing is queued
#   ./deploy/hpc/status.sh <jobid>      # follow one job to completion
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

WATCH=0
JOBID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --watch)   WATCH=1 ;;
        -h|--help) sed -n '2,9p' "$0" >&2; exit 0 ;;
        *)         JOBID="$1" ;;
    esac
    shift
done

require_site
require_hpc_key

if [ -n "$JOBID" ]; then
    hpc_poll "$JOBID" login "${JOB_NAME:-basemap-xyz}"
    exit $?
fi

show() {
    printf '\n%s── compute queue %s\n' "$_C_BLUE" "$_C_OFF" >&2
    hpc_ssh_login "squeue -u '$HPC_USER' -o '%.12i %.20j %.9P %.9T %.10M %.10L %R' 2>/dev/null \
                   || echo '(nothing queued)'" 2>/dev/null

    printf '\n%s── recent %s\n' "$_C_BLUE" "$_C_OFF" >&2
    hpc_ssh_login "sacct -X --starttime now-2days -n \
        -o JobID%14,JobName%20,State%12,Elapsed,ExitCode,MaxRSS 2>/dev/null \
        | grep -i basemap || echo '(no basemap jobs in the last two days)'" 2>/dev/null

    printf '\n%s── shards on scratch %s\n' "$_C_BLUE" "$_C_OFF" >&2
    hpc_ssh_login "
        ls -la '$HPC_OUT' 2>/dev/null | grep -c mbtiles | sed 's/^/    shard files: /' || true
        du -sh '$HPC_OUT' 2>/dev/null | sed 's/^/    /' || echo '    nothing yet'
        if [ -f '$HPC_STATE/xyz.done' ]; then
            echo '    sentinel present -- ready to export'
        else
            echo '    no sentinel yet'
        fi
        df -Pk '$HPC_STAGE' 2>/dev/null | awk 'NR==2 {printf \"    %.0f GB free on scratch\n\", \$4/1048576}'
    " 2>/dev/null

    printf '\n%s── transfer queue %s\n' "$_C_BLUE" "$_C_OFF" >&2
    hpc_ssh_xfer "SLURM_CONF='${HPC_DT_SLURM_CONF:-}' squeue -u '$HPC_USER' \
        -o '%.12i %.20j %.9T %.10M' 2>/dev/null || echo '(none)'" 2>/dev/null
}

if [ "$WATCH" = "0" ]; then show; exit 0; fi
while true; do
    printf '\033[2J\033[H' >&2
    printf '%s  %s%s\n' "$_C_DIM" "$(date -u +%FT%TZ)" "$_C_OFF" >&2
    show
    queued="$(hpc_ssh_login "squeue -u '$HPC_USER' -h 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')"
    [ "${queued:-0}" = "0" ] && { ok "Nothing queued."; break; }
    sleep "${WATCH_INTERVAL:-30}"
done
