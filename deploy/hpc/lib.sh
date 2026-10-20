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
HPC_SITE_VARS_XFER=""

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
        # `|| true`: once the job has left the queue the only line is the
        # sentinel, grep -v matches nothing and exits 1, and under pipefail
        # that killed the driver silently at the very moment its job finished
        # -- twice, on 2026-10-19 and 10-20, the second time on a success.
        line="$(printf '%s\n' "$raw" | grep -v '__SQ_OK__' | head -1 || true)"
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
# Data transfer: initiated here, carried over SSH
# ---------------------------------------------------------------------------
# The site does not allow its machines to connect to external hosts. Its queued
# transfer suite reached object storage from some of its nodes until October
# 2026 -- by accident of their network, not by design -- and the site has since
# said plainly that it is not permitted. So nothing on the cluster connects out:
# every transfer is opened from this side and streamed through SSH to the
# transfer node, object storage -> here -> ssh -> scratch, and back the same
# way. Nothing lands on local disk. See xfer.py, which does the moving, the
# size checks, the retries and the parallel streams.

_hpc_python() {
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        printf '%s' "$PROJECT_DIR/.venv/bin/python"
    elif command -v uv >/dev/null 2>&1; then
        printf '%s' "uv run --frozen --no-dev --project $PROJECT_DIR python"
    else
        printf '%s' "python3"
    fi
}

# hpc_xfer pull|push|check --src ... --dest ... [options]; see xfer.py.
hpc_xfer() {
    require_site
    require_hpc_key
    require_cmd ssh
    local py config
    py="$(_hpc_python)"
    config="$(_hpc_ssh_config)"
    # shellcheck disable=SC2086
    HPC_SSH_CONFIG="$config" HPC_XFER_TARGET="${HPC_USER}@${HPC_XFER_HOST}" \
        $py "$HPC_DIR/xfer.py" "$@"
}

# The bucket as an s3:// URI. One place, so a prefix change is one edit.
hpc_s3() {
    printf 's3://%s/%s' "${BASEMAP_S3_BUCKET:-${HPC_RCLONE_BUCKET:-oceanstream-bucket}}" "${1#/}"
}

confirm() {
    [ "${HPC_ASSUME_YES:-0}" = "1" ] && return 0
    printf '%s  ? %s [y/N] %s' "$_C_YELLOW" "$*" "$_C_OFF" >&2
    local answer; read -r answer
    case "$answer" in y|Y|yes|YES) return 0 ;; *) fail "Cancelled." ;; esac
}
