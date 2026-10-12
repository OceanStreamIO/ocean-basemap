#!/usr/bin/env bash
# =============================================================================
# ocean-basemap → HPC helpers. Sourced by every script here; not executable.
# =============================================================================
# Ported from arco-3d's scripts/hpc/lib.sh, which drives the same allocation.
# Four of its defects are fixed rather than carried over; each is marked FIXED
# below so they do not get reintroduced by a future copy in the other direction.
#
# Portability: must run unchanged on macOS (BSD userland, zsh login shell) and
# in a slim Linux container (no rsync, no GNU-only `date -d`, no `mapfile`, no
# `readlink -f`, no `sed -i` without a backup suffix).
#
# SITE VALUES LIVE OUTSIDE THIS REPO. Every one of them is read from the
# environment with no default, because this repository is published and an
# account name, a hostname or a filesystem path is not ours to publish. Locally
# they come from deploy/hpc/site.env (gitignored); in the cluster they come from
# a ConfigMap. `require_site` names what is missing.
# =============================================================================

# ---------------------------------------------------------------------------
# Logging. Everything to stderr, so stdout stays clean for piping secrets.
# ---------------------------------------------------------------------------
if [ -t 2 ]; then
    _C_BLUE=$'\033[1;34m'; _C_GREEN=$'\033[1;32m'; _C_YELLOW=$'\033[1;33m'
    _C_RED=$'\033[1;31m';  _C_DIM=$'\033[2m';      _C_OFF=$'\033[0m'
else
    _C_BLUE=''; _C_GREEN=''; _C_YELLOW=''; _C_RED=''; _C_DIM=''; _C_OFF=''
fi

log()  { printf '%s==>%s %s\n' "$_C_BLUE"   "$_C_OFF" "$*" >&2; }
ok()   { printf '%s  ✓%s %s\n' "$_C_GREEN"  "$_C_OFF" "$*" >&2; }
warn() { printf '%s  !%s %s\n' "$_C_YELLOW" "$_C_OFF" "$*" >&2; }
info() { printf '%s    %s%s\n' "$_C_DIM"    "$*"      "$_C_OFF" >&2; }
fail() { printf '%s  ✗%s %s\n' "$_C_RED"    "$_C_OFF" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Paths inside this repository
# ---------------------------------------------------------------------------
HPC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HPC_DIR/../.." && pwd)"
SBATCH_DIR="$HPC_DIR/sbatch"

# site.env is optional: in the cluster the same names arrive through envFrom.
#
# Read as defaults, not as overrides. Sourcing it would let the file win over the
# environment, which is backwards twice: it makes overriding one value for a test
# impossible, and where both a file and a ConfigMap exist the file would quietly
# take precedence over the thing that was deployed. So each name is set only if
# it is not already set.
if [ -f "$HPC_DIR/site.env" ]; then
    while IFS= read -r _line; do
        case "$_line" in ''|'#'*) continue ;; *=*) ;; *) continue ;; esac
        _name="${_line%%=*}"
        eval "_current=\${$_name:-}"
        [ -n "$_current" ] || export "$_name=${_line#*=}"
    done < "$HPC_DIR/site.env"
    unset _line _name _current
fi

# ---------------------------------------------------------------------------
# Site contract
# ---------------------------------------------------------------------------
# Required everywhere.
HPC_SITE_VARS="HPC_USER HPC_LOGIN_HOST HPC_XFER_HOST HPC_ACCOUNT HPC_PROJ HPC_SCRATCH"
# Required only by the steps that submit work or move data.
HPC_SITE_VARS_SLURM="HPC_PARTITION HPC_QOS HPC_MODULES"
HPC_SITE_VARS_XFER="HPC_RCLONE_REMOTE HPC_RCLONE_BUCKET HPC_XFER_RCLONE_CMD HPC_DT_SLURM_CONF"

# FIXED (arco: absent). Nothing here has a default, so a half-configured host
# fails by name instead of quietly talking to the wrong cluster.
require_site() {
    local missing="" var value
    for var in $HPC_SITE_VARS "$@"; do
        eval "value=\${$var:-}"
        [ -n "$value" ] || missing="$missing $var"
    done
    [ -z "$missing" ] && return 0
    fail "Site configuration is incomplete. Missing:$missing

  Locally:    cp deploy/hpc/site.env.example deploy/hpc/site.env  and fill it in
              (it is gitignored; the values are in the private infrastructure notes)
  In-cluster: the Job's envFrom ConfigMap supplies these."
}

# Module lists are comma-separated, not space-separated, and that is not a style
# choice. This file has to satisfy two readers with incompatible rules: the shell
# sources it, where `VAR=a b` runs `b` as a command; and `kubectl create configmap
# --from-env-file` reads it, where quotes would end up inside the value. A
# separator that is neither a space nor a quote is the only thing both accept.
hpc_modules() { printf '%s' "${1//,/ }"; }

# Derived layout. A subtree of its own, because the allocation is shared with
# other projects and none of them may be disturbed.
HPC_ROOT="${HPC_ROOT:-${HPC_PROJ:-}/oceanstream/basemap}"
HPC_STAGE="${HPC_STAGE:-${HPC_SCRATCH:-}/oceanstream/basemap}"
HPC_DATA="${HPC_DATA:-$HPC_STAGE/data}"
HPC_OUT="${HPC_OUT:-$HPC_STAGE/out}"
HPC_LOGS="${HPC_LOGS:-$HPC_ROOT/logs}"
HPC_STATE="${HPC_STATE:-$HPC_ROOT/state}"
HPC_BIN="${HPC_BIN:-$HPC_ROOT/bin}"
HPC_VENV="${HPC_VENV:-$HPC_ROOT/code/.venv}"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || fail "Required command not found: $cmd"
    done
}

require_hpc_key() {
    [ -n "${HPC_SSH_KEY:-}" ] || fail "HPC_SSH_KEY is not set (see site.env.example)."
    [ -f "$HPC_SSH_KEY" ] || fail "No SSH key at $HPC_SSH_KEY.

  \$HOME is shared between the login and transfer clusters, so one key in
  authorized_keys covers both. In the cluster the key arrives from a Secret
  mounted at /etc/ssh-hpc; locally, point HPC_SSH_KEY at whichever key is
  already authorised."
}

# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------
# The client config is generated rather than committed: it would otherwise carry
# hostnames. Written once per run into a private temporary directory.
_hpc_ssh_config() {
    if [ -z "${_HPC_SSH_CONF_FILE:-}" ]; then
        _HPC_SSH_CONF_DIR="$(mktemp -d)"
        _HPC_SSH_CONF_FILE="$_HPC_SSH_CONF_DIR/config"
        {
            printf 'Host %s %s\n' "$HPC_LOGIN_HOST" "$HPC_XFER_HOST"
            printf '    User %s\n' "$HPC_USER"
            printf '    IdentityFile %s\n' "$HPC_SSH_KEY"
            printf '    IdentitiesOnly yes\n'
            printf '    BatchMode yes\n'
            printf '    ConnectTimeout 20\n'
            printf '    ServerAliveInterval 30\n'
            printf '    ServerAliveCountMax 60\n'
            # Six round trips per run; multiplexing makes them one connection.
            printf '    ControlMaster auto\n'
            printf '    ControlPath %s/cm-%%r@%%h:%%p\n' "$_HPC_SSH_CONF_DIR"
            printf '    ControlPersist 10m\n'
            # FIXED (arco: accept-new on every pod restart, so a restarted pod
            # would trust whatever answered). A pinned known_hosts is shipped
            # with the key, and without one we say so rather than trusting.
            if [ -n "${HPC_KNOWN_HOSTS:-}" ] && [ -f "$HPC_KNOWN_HOSTS" ]; then
                printf '    UserKnownHostsFile %s\n' "$HPC_KNOWN_HOSTS"
                printf '    StrictHostKeyChecking yes\n'
            else
                printf '    UserKnownHostsFile %s/known_hosts\n' "$_HPC_SSH_CONF_DIR"
                printf '    StrictHostKeyChecking accept-new\n'
            fi
        } > "$_HPC_SSH_CONF_FILE"
        [ -n "${HPC_KNOWN_HOSTS:-}" ] && [ -f "$HPC_KNOWN_HOSTS" ] \
            || warn "HPC_KNOWN_HOSTS is not set; trusting host keys on first use."
    fi
    printf '%s' "$_HPC_SSH_CONF_FILE"
}

# FIXED (arco: _hpc_ssh_opts called fail() inside a process substitution, so a
# missing key killed only the subshell and ssh then ran with the caller's
# ambient defaults — a silent fallback to the wrong identity). The check is at
# the top level of the wrappers instead, where exit works.
# shellcheck disable=SC2029
hpc_ssh_login() {
    require_site
    require_hpc_key
    ssh -F "$(_hpc_ssh_config)" "${HPC_USER}@${HPC_LOGIN_HOST}" "$@"
}

# shellcheck disable=SC2029
hpc_ssh_xfer() {
    require_site
    require_hpc_key
    ssh -F "$(_hpc_ssh_config)" "${HPC_USER}@${HPC_XFER_HOST}" "$@"
}

# Send a local directory as a tar stream. rsync is deliberately not used: it is
# absent from the container this runs in, and the compute cluster has no route
# to a git remote to pull from instead.
hpc_send_tree() {
    local src="$1" dest="$2"; shift 2
    require_cmd tar ssh
    ( cd "$src" && tar -czf - "$@" ) \
        | hpc_ssh_login "mkdir -p '$dest' && tar -xzf - -C '$dest' && echo ok" >/dev/null \
        || fail "Could not send $src to $dest"
}

# ---------------------------------------------------------------------------
# Slurm
# ---------------------------------------------------------------------------
# Reasons a job will never start. Polling through them wastes hours: an array
# whose dependency can no longer be satisfied sits in PENDING until the poll
# timeout, looking exactly like a busy queue.
# FIXED (arco: polled until HPC_POLL_TIMEOUT, which defaults to a day).
_HPC_DEAD_REASONS='DependencyNeverSatisfied|JobHeldAdmin|JobHeldUser|launch failed|InvalidQOS|InvalidAccount|PartitionConfig'

# Exit code from the accounting database. squeue forgets a job seconds after it
# ends; sacct does not.
# FIXED (arco: never ran sacct at all, so success was whatever the job script
# happened to echo — and its `rc=$?` after a command under `set -e` was dead
# code, so a failing job printed no exit line and the caller read the absence
# as success.)
hpc_job_exit() {
    local jobid="$1" where="${2:-login}" ssh_fn="hpc_ssh_login" env=""
    [ "$where" = "xfer" ] && { ssh_fn="hpc_ssh_xfer"; env="SLURM_CONF='$HPC_DT_SLURM_CONF' "; }
    "$ssh_fn" "${env}sacct -j '$jobid' -X -n -o ExitCode,State 2>/dev/null | head -1" 2>/dev/null \
        | tr -d '[:space:]'
}

# Submit an sbatch script (on stdin), poll to completion, print its log, and
# return its real exit status.
#   $1 = login|xfer   $2 = job name   [$3.. = extra sbatch args]
# HPC_SUBMIT_NOWAIT=1 submits and returns immediately, printing the job id.
hpc_submit_wait() {
    local where="$1" job_name="$2"; shift 2
    local ssh_fn="hpc_ssh_login" env=""
    [ "$where" = "xfer" ] && { ssh_fn="hpc_ssh_xfer"; env="SLURM_CONF='$HPC_DT_SLURM_CONF' "; }
    local remote_script="$HPC_ROOT/tmp/${job_name}.sbatch"

    "$ssh_fn" "mkdir -p '$HPC_ROOT/tmp' '$HPC_LOGS' && cat > '$remote_script'" \
        || fail "Could not stage $remote_script"

    # The login shell prints a module banner on stderr for every connection, so
    # stderr stays out of the capture and the id is parsed defensively.
    local submit_out submit_err jobid
    submit_err="$(mktemp)"
    submit_out="$("$ssh_fn" "${env}sbatch --parsable $* '$remote_script'" 2>"$submit_err")" || true
    jobid="$(printf '%s' "$submit_out" | tr -d '[:space:]' | grep -oE '^[0-9]+' | tail -1)"
    if [ -z "$jobid" ]; then
        warn "sbatch produced no job id. stdout: ${submit_out:-<empty>}"
        sed 's/^/      /' "$submit_err" >&2
        rm -f "$submit_err"
        fail "Submission failed for $job_name"
    fi
    rm -f "$submit_err"

    log "Job $jobid submitted ($job_name)"
    printf '%s\n' "$jobid" > "${HPC_JOBID_FILE:-/dev/null}"
    if [ "${HPC_SUBMIT_NOWAIT:-0}" = "1" ]; then
        info "HPC_SUBMIT_NOWAIT=1 — not waiting. Reattach with: ./deploy/hpc/status.sh $jobid"
        printf 'jobid=%s\nexit=pending\n' "$jobid"
        return 0
    fi

    hpc_poll "$jobid" "$where" "$job_name"
}

# Poll one job to completion, then print its log and report its exit code.
hpc_poll() {
    local jobid="$1" where="${2:-login}" job_name="${3:-job}"
    local ssh_fn="hpc_ssh_login" env=""
    [ "$where" = "xfer" ] && { ssh_fn="hpc_ssh_xfer"; env="SLURM_CONF='$HPC_DT_SLURM_CONF' "; }

    local waited=0 interval="${HPC_POLL_INTERVAL:-15}" timeout="${HPC_POLL_TIMEOUT:-172800}"
    local misses=0 allowed="${HPC_POLL_MISSES:-40}"
    local raw line state reason last=""
    while [ "$waited" -lt "$timeout" ]; do
        # squeue exits 1 once a job leaves the queue. That is the loop's exit
        # condition, not an error, so it must not trip `set -e`.
        #
        # ssh's own failures are a different thing entirely and this loop used to
        # confuse the two, in both directions. A dropped connection returns 255,
        # and because this is a plain assignment its status is the assignment's,
        # so `set -e` killed the driver silently in the middle of a wait that was
        # meant to last hours -- measured, seventy-five seconds into one. Had it
        # survived that, the empty output would then have read as "the job has
        # left the queue", which is worse: the driver would have gone on to
        # export a render that had not happened.
        #
        # So the remote side ends in a sentinel. Output carrying it means ssh
        # worked and squeue genuinely said nothing; output without it means the
        # connection failed and the right response is to try again. A cluster
        # login node refuses a connection now and then, and that must cost one
        # poll rather than the run.
        raw="$("$ssh_fn" "${env}squeue -j '$jobid' -h -o '%T|%R' 2>/dev/null; echo __SQ_OK__" \
               2>/dev/null)" || true
        case "$raw" in
            *__SQ_OK__*) misses=0 ;;
            *)
                misses=$((misses + 1))
                if [ "$misses" -ge "$allowed" ]; then
                    fail "Lost contact with $where for $((misses * interval))s while waiting on $jobid.
  The job itself may still be running: ./deploy/hpc/status.sh $jobid"
                fi
                printf '%s    unreachable, retrying (%s/%s)%s\r' \
                       "$_C_DIM" "$misses" "$allowed" "$_C_OFF" >&2
                sleep "$interval"; waited=$((waited + interval))
                continue ;;
        esac
        line="$(printf '%s\n' "$raw" | grep -v '__SQ_OK__' | head -1)"
        [ -z "$line" ] && break
        state="${line%%|*}"; reason="${line#*|}"
        if printf '%s' "$reason" | grep -qE "$_HPC_DEAD_REASONS"; then
            printf '\033[2K' >&2
            "$ssh_fn" "${env}scancel '$jobid'" >/dev/null 2>&1 || true
            fail "Job $jobid will never start: $reason
  Cancelled it rather than polling for a day. Check the jobs it depends on."
        fi
        if [ "$state|$reason" != "$last" ]; then
            info "$state${reason:+ — $reason}"
            last="$state|$reason"
        else
            printf '%s    %s %ss elapsed%s\r' "$_C_DIM" "$state" "$waited" "$_C_OFF" >&2
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done
    printf '\033[2K' >&2
    [ "$waited" -ge "$timeout" ] && warn "Stopped polling after ${timeout}s; job $jobid may still be queued."

    # Logs are named for the job so they can be found without asking Slurm.
    "$ssh_fn" "tail -n ${HPC_LOG_TAIL:-40} '$HPC_LOGS/${job_name}-${jobid}.out' 2>/dev/null \
               || tail -n ${HPC_LOG_TAIL:-40} '$HPC_LOGS'/${job_name}-${jobid}_*.out 2>/dev/null \
               || echo '(no log for $job_name-$jobid)'" 2>/dev/null

    local verdict; verdict="$(hpc_job_exit "$jobid" "$where")"
    case "$verdict" in
        0:0COMPLETED) ok "Job $jobid COMPLETED"; return 0 ;;
        "")           warn "Job $jobid left no accounting record"; return 1 ;;
        *)            fail "Job $jobid finished as $verdict" ;;
    esac
}

# Validate a rendered sbatch without queueing it. Catches a bad QOS, an
# over-long walltime or a partition this account cannot use, in a second.
# FIXED (arco: had --render-only but never asked Slurm whether it would accept).
hpc_test_only() {
    local job_name="$1"; shift
    local remote="$HPC_ROOT/tmp/${job_name}.test.sbatch"
    hpc_ssh_login "mkdir -p '$HPC_ROOT/tmp' && cat > '$remote'" || fail "Could not stage $remote"
    if hpc_ssh_login "sbatch --test-only $* '$remote' 2>&1"; then
        ok "$job_name would be accepted"
        return 0
    fi
    # Returns rather than exits: callers run this inside a command substitution,
    # where an exit would kill only the subshell and the caller would carry on as
    # though it had passed.
    warn "$job_name was rejected by the scheduler (see above)"
    return 1
}

# ---------------------------------------------------------------------------
# Data transfer
# ---------------------------------------------------------------------------
# Some of the transfer nodes have no resolver. Observed: of three jobs submitted
# within an hour, two landed on nodes that reached object storage and one landed
# on a node where every request failed with "network is unreachable ... on
# [::1]:53" -- no DNS at all. The queued wrapper builds a fixed sbatch line with
# no way to choose or exclude a node, so which one a transfer gets is luck.
#
# Retrying is the whole mitigation, and it is a sound one: every transfer here is
# `copy`, which is resumable and idempotent, so a retry continues rather than
# restarts and draws a fresh node.
_HPC_NO_DNS='network is unreachable|no such host|Temporary failure in name resolution|lookup .* on \[::1\]:53'

# Outbound network access is only available through the site's queued transfer
# suite, on its own Slurm cluster and its own nodes. A plain batch job has no
# resolver at all, which looks like a firewall problem and is not one.
hpc_rclone() {
    local label="$1"; shift
    require_site $HPC_SITE_VARS_XFER
    local attempt=1 limit="${HPC_XFER_ATTEMPTS:-4}"
    while : ; do
        if _hpc_rclone_once "$label (attempt $attempt/$limit)" "$@"; then
            return 0
        fi
        if [ "${_HPC_XFER_RETRYABLE:-0}" != "1" ]; then
            return 1
        fi
        if [ "$attempt" -ge "$limit" ]; then
            fail "$label: $limit attempts all landed on a transfer node that could
  not reach the object store -- either refused outright or stalled with no
  bytes moving. That is bad luck rather than a configuration problem: the
  queued wrapper picks the node and offers no way to exclude one. Try again,
  or ask the site which nodes in the archive partition have outbound DNS."
        fi
        warn "retrying on another transfer node"
        attempt=$((attempt + 1))
    done
}

_hpc_rclone_once() {
    local label="$1"; shift
    log "Queueing $label"
    info "rclone $*"
    local submitted jobid
    # The wrapper writes its logs into the submitting directory, so run from the
    # log directory to keep them out of $HOME.
    submitted="$(hpc_ssh_xfer "mkdir -p '$HPC_LOGS' && cd '$HPC_LOGS' && ${HPC_XFER_RCLONE_CMD} $*" 2>/dev/null)"
    jobid="$(printf '%s' "$submitted" | grep -oE '[0-9]+' | tail -1)"
    [ -n "$jobid" ] || { warn "no job id from ${HPC_XFER_RCLONE_CMD}: ${submitted:-<empty>}"; return 1; }
    log "Transfer job $jobid submitted"

    if [ "${HPC_SUBMIT_NOWAIT:-0}" = "1" ]; then
        printf 'jobid=%s\nexit=pending\n' "$jobid"
        return 0
    fi

    local waited=0 interval="${HPC_POLL_INTERVAL:-15}" timeout="${HPC_XFER_TIMEOUT:-172800}"
    local misses=0 allowed="${HPC_POLL_MISSES:-40}" raw state
    # A node without a resolver does not fail: it sits in RUNNING, writes no
    # bytes and logs nothing, because the wrapper only flushes its stderr at
    # the end. Twice in a row that burned the full eighteen-hour limit while
    # looking healthy in squeue. So progress is measured directly, and a
    # transfer that has moved nothing for long enough is cancelled and retried
    # -- which is what the pattern match below cannot do, since there is no
    # message to match until the job is over.
    local watch="${HPC_XFER_WATCH:-}" size last_size="" last_change=0
    local stall_after="${HPC_XFER_STALL_AFTER:-480}"
    local stall_every="${HPC_XFER_STALL_CHECK:-120}" next_check="${HPC_XFER_STALL_CHECK:-120}"
    _HPC_XFER_RETRYABLE=0
    while [ "$waited" -lt "$timeout" ]; do
        # The same sentinel the login poll uses, for the same reason: an empty
        # answer has to mean "the queue says nothing", not "the connection
        # failed". Here a dropped connection ended the wait and then found no
        # accounting record either, so a transfer that was running perfectly
        # well was reported as "exit unknown" -- and this one moves hundreds of
        # gigabytes over hours, which is long enough to expect a hiccup.
        raw="$(hpc_ssh_xfer "SLURM_CONF='$HPC_DT_SLURM_CONF' squeue -j '$jobid' -h -o '%T' 2>/dev/null; echo __SQ_OK__" 2>/dev/null)" || true
        case "$raw" in
            *__SQ_OK__*) misses=0 ;;
            *)
                misses=$((misses + 1))
                if [ "$misses" -ge "$allowed" ]; then
                    warn "$label: lost contact with the transfer queue for $((misses * interval))s"
                    _HPC_XFER_RETRYABLE=1
                    return 1
                fi
                sleep "$interval"; waited=$((waited + interval))
                continue ;;
        esac
        state="$(printf '%s\n' "$raw" | grep -v '__SQ_OK__' | tr -d '[:space:]')"
        [ -z "$state" ] && break
        if [ -n "$watch" ] && [ "$state" = "RUNNING" ] && [ "$waited" -ge "$next_check" ]; then
            next_check=$((waited + stall_every))
            # Capped: du over a tree of tens of thousands of cells can take
            # minutes on GPFS, and a slow measurement must not be read as a
            # stalled transfer. A sample that does not come back is skipped.
            size="$(hpc_ssh_xfer "timeout 60 du -sb '$watch' 2>/dev/null | cut -f1" 2>/dev/null | tr -cd '0-9')"
            if [ -n "$size" ]; then
                if [ "$size" != "$last_size" ]; then
                    last_size="$size"; last_change="$waited"
                elif [ $((waited - last_change)) -ge "$stall_after" ]; then
                    printf '\033[2K' >&2
                    warn "$label has moved no bytes in $((waited - last_change))s; cancelling $jobid"
                    hpc_ssh_xfer "SLURM_CONF='$HPC_DT_SLURM_CONF' scancel '$jobid'" >/dev/null 2>&1 || true
                    _HPC_XFER_RETRYABLE=1
                    return 1
                fi
            fi
        fi
        printf '%s    %s %ss elapsed%s\r' "$_C_DIM" "$state" "$waited" "$_C_OFF" >&2
        sleep "$interval"; waited=$((waited + interval))
    done
    printf '\033[2K' >&2

    local code
    code="$(hpc_ssh_xfer "SLURM_CONF='$HPC_DT_SLURM_CONF' sacct -j '$jobid' -X -n -o ExitCode 2>/dev/null | head -1" 2>/dev/null | tr -d '[:space:]')"
    code="${code%%:*}"
    # 8 is "max transfer limit reached", which is how a capped pilot ends.
    case "${code:-none}" in
        0) ok "$label complete"; return 0 ;;
        8) ok "$label hit its transfer cap (expected for a pilot)"; return 0 ;;
        *) local errors
           errors="$(hpc_ssh_xfer "tail -n 40 '$HPC_LOGS'/*${jobid}*.err 2>/dev/null" 2>/dev/null)"
           printf '%s\n' "$errors" | sed 's/^/      /' >&2
           if printf '%s' "$errors" | grep -qE "$_HPC_NO_DNS"; then
               _HPC_XFER_RETRYABLE=1
               info "job $jobid ran on $(hpc_ssh_xfer "SLURM_CONF='$HPC_DT_SLURM_CONF' sacct -j '$jobid' -X -n -o NodeList 2>/dev/null | head -1" 2>/dev/null | tr -d '[:space:]')"
           else
               _HPC_XFER_RETRYABLE=0
           fi
           warn "$label failed (exit ${code:-unknown})"; return 1 ;;
    esac
}

# The bucket as rclone addresses it. One place, so a prefix change is one edit.
hpc_remote() {
    printf '%s:%s/%s' "$HPC_RCLONE_REMOTE" "$HPC_RCLONE_BUCKET" "${1#/}"
}

confirm() {
    [ "${HPC_ASSUME_YES:-0}" = "1" ] && return 0
    printf '%s  ? %s [y/N] %s' "$_C_YELLOW" "$*" "$_C_OFF" >&2
    local answer; read -r answer
    case "$answer" in y|Y|yes|YES) return 0 ;; *) fail "Cancelled." ;; esac
}
