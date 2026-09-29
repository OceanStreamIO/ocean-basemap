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
XYZ_TIME="${XYZ_TIME:-02:00:00}"
XYZ_CACHEMAX="${XYZ_CACHEMAX:-64}"
VERIFY_TIME="${VERIFY_TIME:-01:00:00}"

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
if [ "$MAX_ZOOM" -ge 14 ]; then
    warn "That is a very large tree. Sending it is the constraint, not rendering it:"
    warn "  at the measured ~1,930 objects a second from one publisher pod, it is"
    warn "  $(awk -v t="$TREE_TILES" 'BEGIN{printf "%.0f", t/1930/3600}') hours of publishing, and roughly"
    warn "  $(awk -v t="$TREE_TILES" 'BEGIN{printf "%.0f", t*600/1e9}') GB of objects to store and later delete."
    warn "  Deep levels are only sharper where an archive actually holds that zoom."
    confirm "Render z0-$MAX_ZOOM anyway?"
fi

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# One sed per template. Safe here because no substituted value is a secret and
# none contains an ampersand; anything credential-bearing must not come this way.
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
        -e "s|@WALLTIME@|${RENDER_TIME:-}|g" \
        -e "s|@GDAL_CACHEMAX@|${RENDER_CACHEMAX:-}|g" \
        -e "s|@SHARDS@|${SHARDS}|g" \
        -e "s|@MAX_ZOOM@|${MAX_ZOOM}|g" \
        -e "s|@ARRAY@|0-$((SHARDS - 1))|g" \
        -e "s|@OPTIONAL@|${OPTIONAL# }|g" \
        "$SBATCH_DIR/$template" \
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

case "$FROM" in
    vector|raster)
        # The only stage that wants a high-memory node: see the template.
        RENDER_CPUS="$RASTER_CPUS" RENDER_TIME="$RASTER_TIME" \
        RENDER_WORKERS_N="$RASTER_WORKERS" RENDER_CACHEMAX="$RASTER_CACHEMAX" \
        RENDER_CONSTRAINT="${HPC_CONSTRAINT:-}" \
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

RENDER_TIME="$VERIFY_TIME" RENDER_CONSTRAINT="" RENDER_CPUS=4 \
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
