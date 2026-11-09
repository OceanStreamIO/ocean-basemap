#!/usr/bin/env bash
# =============================================================================
# Render and submit the build as a dependency chain
# =============================================================================
#   vector-a ─┐
#             ├─► raster ─► xyz[0..N-1] ─► verify ─► (sentinel)
#   vector-b ─┘
#
# The two vector groups share no inputs and run at once. Everything downstream
# waits on both. The export to object storage is NOT in this chain: it runs on a
# different Slurm cluster, so nothing here can depend on it and nothing there can
# depend on this. The verify job writes a sentinel and export-tiles.sh reads it.
#
#   ./deploy/hpc/submit.sh                    # the whole chain
#   ./deploy/hpc/submit.sh --test-only        # ask the scheduler, queue nothing
#   ./deploy/hpc/submit.sh --render-only      # print the scripts, connect to nothing
#   ./deploy/hpc/submit.sh --from xyz         # resubmit from one stage on
#   ./deploy/hpc/submit.sh --shards 8         # a different array width
#   ./deploy/hpc/submit.sh --with emodnet --with hr-bathymetry
#
# Re-running is safe. Every job is named deterministically and an existing job
# of the same name is adopted rather than submitted again -- two xyz arrays
# writing the same shard files would corrupt each other.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/hpc/lib.sh
. "$SCRIPT_DIR/lib.sh"

SHARDS="${XYZ_SHARDS:-4}"
# The deepest level of the flat tree. Each extra level is four times the
# tiles, so this is the single most expensive number here.
MAX_ZOOM="${XYZ_MAX_ZOOM:-12}"
TEST_ONLY=0
RENDER_ONLY=0
FROM="vector"
OPTIONAL=""
DETACH="${HPC_SUBMIT_NOWAIT:-0}"

# Sizing. Cores are whole-node where the work parallelises and smaller where it
# does not; walltimes are deliberately tight, because a short job is easier for
# the scheduler to fit into a gap.
VECTOR_CPUS="${VECTOR_CPUS:-32}"
VECTOR_TIME="${VECTOR_TIME:-12:00:00}"
RASTER_CPUS="${RASTER_CPUS:-112}"
RASTER_WORKERS="${RASTER_WORKERS:-96}"
RASTER_TIME="${RASTER_TIME:-12:00:00}"
RASTER_CACHEMAX="${RASTER_CACHEMAX:-512}"
XYZ_CPUS="${XYZ_CPUS:-112}"
XYZ_WORKERS="${XYZ_WORKERS:-112}"
XYZ_CACHEMAX="${XYZ_CACHEMAX:-64}"
# XYZ_TIME and VERIFY_TIME are derived from the tree below, not set here: a
# constant here is a constant for one depth, and every level is four times the
# last.

while [ $# -gt 0 ]; do
    case "$1" in
        --shards)      SHARDS="${2:?--shards needs a number}"; shift ;;
        --max-zoom)    MAX_ZOOM="${2:?--max-zoom needs a number}"; shift ;;
        --test-only)   TEST_ONLY=1 ;;
        --render-only) RENDER_ONLY=1 ;;
        --from)        FROM="${2:?--from needs a stage}"; shift ;;
        --with)        OPTIONAL="$OPTIONAL ${2:?--with needs a step}"; shift ;;
        --detach)      DETACH=1 ;;
        -h|--help)     sed -n '2,25p' "$0" >&2; exit 0 ;;
        *)             fail "Unknown argument: $1" ;;
    esac
    shift
done

case "$FROM" in vector|raster|xyz|verify) ;; *) fail "--from must be vector, raster, xyz or verify" ;; esac
[ "$SHARDS" -ge 1 ] 2>/dev/null || fail "--shards must be a positive number"
[ "$MAX_ZOOM" -ge 0 ] 2>/dev/null || fail "--max-zoom must be a number"

# Say what the depth costs before doing it, because a mistyped digit here is not
# a small mistake: every level is four times the tiles of the one above, and the
# render is the cheap half -- the objects have to be sent one at a time
# afterwards, and then stored, and then deleted if the build is wrong.
TREE_TILES="$(awk -v z="$MAX_ZOOM" 'BEGIN{t=0; for(i=0;i<=z;i++) t+=4^i; printf "%.0f", t}')"
log "z0-$MAX_ZOOM is $TREE_TILES tiles"

# Walltime follows the tree, because the tree quadruples with every level and a
# fixed limit is a limit for whichever depth it was written at. Measured on the
# z13 run: 16 shards of 5,592,415 tiles each finished in 15m40s on 112 cores,
# which is 5,950 tiles a second per node, and the verify job read all 89,478,485
# in 50 minutes, or 29,800 a second. Both are multiplied for headroom -- a job
# that overruns loses everything it rendered, and asking for an hour too many
# only makes it slightly harder to schedule.
#
# The verify figure did not carry over. On the z12 tree with detail zooms
# (2026-10-19) it checked one 11.5 GB shard in 15-18 minutes, about 6,000 tiles
# a second, because z13's tiles were small open ocean and these are not; four
# in a row ran out of the hour. It now checks the shards side by side, so the
# rate is per process and the limit divides by how many run at once.
#
# The floors keep today's shallow-tree behaviour: at z12 on four shards this
# still asks for the two hours it always did.
VERIFY_CPUS="${VERIFY_CPUS:-32}"
hms() { awk -v s="$1" 'BEGIN{h=int(s/3600); printf "%02d:%02d:00", h, int((s-h*3600)/60)}'; }
XYZ_TIME="${XYZ_TIME:-$(hms "$(awk -v t="$TREE_TILES" -v n="$SHARDS" \
    'BEGIN{s=t/n/5950*3; print (s<7200 ? 7200 : int(s/600+1)*600)}')")}"
VERIFY_TIME="${VERIFY_TIME:-$(hms "$(awk -v t="$TREE_TILES" -v n="$SHARDS" -v c="$VERIFY_CPUS" \
    'BEGIN{p=(n<c ? n : c); s=t/(6000*p)*2; print (s<3600 ? 3600 : int(s/600+1)*600)}')")}"
log "walltime: xyz $XYZ_TIME per shard, verify $VERIFY_TIME"
if [ "$MAX_ZOOM" -ge 14 ]; then
    warn "That is a very large tree. Sending it is the constraint, not rendering it:"
    warn "  at the measured ~1,930 objects a second from one publisher pod, it is"
    warn "  $(awk -v t="$TREE_TILES" 'BEGIN{printf "%.0f", t/1930/3600}') hours of publishing, and roughly"
    warn "  $(awk -v t="$TREE_TILES" 'BEGIN{printf "%.0f", t*600/1e9}') GB of objects to store and later delete."
    warn "  Deep levels are only sharper where an archive actually holds that zoom."
    confirm "Render z0-$MAX_ZOOM anyway?"
fi

# The sentinel says "the shards on disk are complete and add up". A render that
# is about to overwrite those shards makes that false, so it goes first -- the
# alternative is an export that starts the moment it is asked, against a
# directory being rewritten under it, which is exactly what happened once.
if [ "$FROM" = "vector" ] || [ "$FROM" = "raster" ] || [ "$FROM" = "xyz" ]; then
    hpc_ssh_login "rm -f '$HPC_STATE/xyz.done'" >/dev/null 2>&1 || true
    log "cleared the previous sentinel"
fi

# ---------------------------------------------------------------------------
# Rendering
# Settings the pipeline reads from its environment, carried into the batch
# scripts. The scheduler starts them in an environment of its own, so nothing
# set here reaches them otherwise -- and an unset one is not an error: a tree
# asked for with detail zooms would simply come out without them. Named rather
# than "every BASEMAP_*", because this machine's environment also holds
# endpoints and paths that mean something else on the cluster.
FORWARD="${HPC_FORWARD:-BASEMAP_BATHY_ARCHIVES BASEMAP_LAKE_ARCHIVES BASEMAP_LAND_STYLE BASEMAP_XYZ_DETAIL_MAX_ZOOM}"
EXPORTS=""
for var in $FORWARD; do
    case "$var" in *[!A-Z0-9_]*|"") fail "HPC_FORWARD names variables; '$var' is not one" ;; esac
    value="${!var:-}"
    [ -n "$value" ] || continue
    case "$value" in
        *[!A-Za-z0-9_\ .,:/-]*) fail "$var holds characters that cannot go into a batch script: $value" ;;
    esac
    EXPORTS="${EXPORTS}export $var='$value'\\n"
    log "forwarding $var='$value'"
done

# ---------------------------------------------------------------------------
# One sed per template. Safe here because no substituted value is a secret and
# none contains an ampersand; anything credential-bearing must not come this way.
# The forwarded settings replace the template's "# @EXPORTS@" line; awk turns
# the \n separators into lines.
render() {
    local template="$1"
    sed -e "s|@ACCOUNT@|${HPC_ACCOUNT:-}|g" \
        -e "s|@QOS@|${HPC_QOS:-}|g" \
        -e "s|@PARTITION@|${HPC_PARTITION:-}|g" \
        -e "s|@CONSTRAINT@|${RENDER_CONSTRAINT:-}|g" \
        -e "s|@ROOT@|${HPC_ROOT}|g" \
        -e "s|@LOGS@|${HPC_LOGS}|g" \
        -e "s|@STATE@|${HPC_STATE}|g" \
        -e "s|@OUT@|${HPC_OUT}|g" \
        -e "s|@CPUS@|${RENDER_CPUS:-}|g" \
        -e "s|@WORKERS@|${RENDER_WORKERS_N:-}|g" \
        -e "s|@RASTER_STEPS@|${RASTER_STEPS:-hypsometric bathymetry globe}|g" \
        -e "s|@WALLTIME@|${RENDER_TIME:-}|g" \
        -e "s|@GDAL_CACHEMAX@|${RENDER_CACHEMAX:-}|g" \
        -e "s|@SHARDS@|${SHARDS}|g" \
        -e "s|@MAX_ZOOM@|${MAX_ZOOM}|g" \
        -e "s|@ARRAY@|0-$((SHARDS - 1))|g" \
        -e "s|@OPTIONAL@|${OPTIONAL# }|g" \
        "$SBATCH_DIR/$template" \
    | awk -v exports="$EXPORTS" '$0 == "# @EXPORTS@" { printf "%s", exports; next } { print }' \
    | if [ -z "${RENDER_CONSTRAINT:-}" ]; then
          # An empty --constraint= is rejected outright, so drop the line
          # rather than leave it blank.
          grep -v '^#SBATCH --constraint=$'
      else
          cat
      fi
}

# Adopt an already-queued job of this name instead of submitting a second one.
existing_job() {
    [ "$RENDER_ONLY" = "1" ] && return 1
    hpc_ssh_login "squeue -u '$HPC_USER' -h -n '$1' -o '%i' 2>/dev/null | head -1" 2>/dev/null \
        | tr -d '[:space:]' | grep -E '^[0-9]+' || true
}

# $1 template, $2 job name, $3.. extra sbatch args. Echoes the job id.
stage() {
    local template="$1" name="$2"; shift 2
    local rendered found
    rendered="$(render "$template")"

    if [ "$RENDER_ONLY" = "1" ]; then
        printf '\n===== %s (%s) =====\n' "$name" "$template" >&2
        printf '%s\n' "$rendered" >&2
        printf 'rendered-%s\n' "$name"
        return 0
    fi
    if [ "$TEST_ONLY" = "1" ]; then
        # Without the dependency arguments: nothing has been submitted, so there
        # is no job id to depend on, and a non-numeric one makes the scheduler
        # reject the stage for that reason rather than on its own merits.
        printf '%s\n' "$rendered" | hpc_test_only "$name" >/dev/null || return 1
        printf 'tested-%s\n' "$name"
        return 0
    fi
    found="$(existing_job "$name")"
    if [ -n "$found" ]; then
        warn "$name is already queued as $found; adopting it rather than submitting again"
        printf '%s\n' "$found"
        return 0
    fi
    HPC_SUBMIT_NOWAIT=1 printf '%s\n' "$rendered" \
        | HPC_SUBMIT_NOWAIT=1 hpc_submit_wait login "$name" "$@" \
        | grep '^jobid=' | cut -d= -f2
}

if [ "$RENDER_ONLY" = "0" ]; then
    require_site $HPC_SITE_VARS_SLURM
    require_hpc_key
    hpc_ssh_login "test -f '$HPC_ROOT/etc/basemap-env.sh'" 2>/dev/null \
        || fail "The cluster environment is not set up. Run ./deploy/hpc/bootstrap.sh first."
fi

log "Submitting the build: $SHARDS xyz shards, from '$FROM'${OPTIONAL:+, with$OPTIONAL}"

DEPS=""
JA=""; JB=""; JC=""; JD=""; JE=""

if [ "$FROM" = "vector" ]; then
    RENDER_CPUS="$VECTOR_CPUS" RENDER_TIME="$VECTOR_TIME" RENDER_CONSTRAINT="" \
        JA="$(stage 10-vector-a.sbatch basemap-vector-a)" || fail "basemap-vector-a did not get through"
    RENDER_CPUS="$VECTOR_CPUS" RENDER_TIME="$VECTOR_TIME" RENDER_CONSTRAINT="" \
        JB="$(stage 20-vector-b.sbatch basemap-vector-b)" || fail "basemap-vector-b did not get through"
    DEPS="--dependency=afterok:$JA:$JB"
fi

# RASTER_CONSTRAINT: "site" (the default) takes the site's node type, "none"
# asks for a standard node -- right when the job has nothing heavy left to do,
# such as rebuilding the mosaics around archives that are already built, and a
# high-memory node would mean waiting a day for minutes of work.
case "${RASTER_CONSTRAINT:-site}" in
    site) raster_constraint="${HPC_CONSTRAINT:-}" ;;
    none) raster_constraint="" ;;
    *)    raster_constraint="$RASTER_CONSTRAINT" ;;
esac
case "$FROM" in
    vector|raster)
        # The only stage that wants a high-memory node: see the template.
        RENDER_CPUS="$RASTER_CPUS" RENDER_TIME="$RASTER_TIME" \
        RENDER_WORKERS_N="$RASTER_WORKERS" RENDER_CACHEMAX="$RASTER_CACHEMAX" \
        RENDER_CONSTRAINT="$raster_constraint" \
            JC="$(stage 30-raster.sbatch basemap-raster ${DEPS:+$DEPS})" \
                || fail "basemap-raster did not get through"
        DEPS="--dependency=afterok:$JC"
        ;;
esac

case "$FROM" in
    vector|raster|xyz)
        RENDER_CPUS="$XYZ_CPUS" RENDER_TIME="$XYZ_TIME" \
        RENDER_WORKERS_N="$XYZ_WORKERS" RENDER_CACHEMAX="$XYZ_CACHEMAX" \
        RENDER_CONSTRAINT="" \
            JD="$(stage 40-xyz.sbatch basemap-xyz ${DEPS:+$DEPS})" \
                || fail "basemap-xyz did not get through"
        DEPS="--dependency=afterok:$JD"
        ;;
esac

RENDER_TIME="$VERIFY_TIME" RENDER_CONSTRAINT="" RENDER_CPUS="$VERIFY_CPUS" \
    JE="$(stage 50-verify.sbatch basemap-verify ${DEPS:+$DEPS})" \
        || fail "basemap-verify did not get through"

if [ "$RENDER_ONLY" = "1" ]; then ok "Rendered; nothing was submitted."; exit 0; fi
if [ "$TEST_ONLY" = "1" ]; then ok "The scheduler would accept every stage."; exit 0; fi

printf '\n' >&2
[ -n "$JA" ] && info "vector-a  $JA"
[ -n "$JB" ] && info "vector-b  $JB"
[ -n "$JC" ] && info "raster    $JC"
[ -n "$JD" ] && info "xyz       $JD  (array 0-$((SHARDS - 1)))"
info "verify    $JE"

# Recorded so a later invocation, or a restarted driver pod, can find the run
# again without asking the scheduler to list everything.
hpc_ssh_login "mkdir -p '$HPC_STATE' && cat > '$HPC_STATE/last-run.json'" <<JSONEOF
{
  "submitted": "$(date -u +%FT%TZ)",
  "shards": $SHARDS,
  "optional": "${OPTIONAL# }",
  "jobs": {"vector_a": "$JA", "vector_b": "$JB", "raster": "$JC",
           "xyz": "$JD", "verify": "$JE"}
}
JSONEOF

ok "Submitted"
if [ "$DETACH" = "1" ]; then
    log "Detached. Follow it with: ./deploy/hpc/status.sh"
    log "When verify completes:    ./deploy/hpc/export-tiles.sh"
    exit 0
fi
log "Waiting on verify ($JE). Ctrl-C is safe -- the jobs keep running."
hpc_poll "$JE" login basemap-verify
log "Next: ./deploy/hpc/export-tiles.sh"
