#!/bin/bash
# =============================================================================
# Check the shards add up, then write the sentinel the driver waits on.
# =============================================================================
# The export cannot be a scheduler dependency of the array: object storage is
# reachable only from a different Slurm cluster, so nothing there can depend on
# a job here. This job writes a sentinel instead and the driver comes back for
# it, which is what makes the driver re-entrant rather than one long script.
#SBATCH --job-name=basemap-verify
#SBATCH --account=@ACCOUNT@
#SBATCH --qos=@QOS@
#SBATCH --partition=@PARTITION@
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --time=@WALLTIME@
#SBATCH --output=@LOGS@/basemap-verify-%j.out
#SBATCH --error=@LOGS@/basemap-verify-%j.out

set -euo pipefail
. "@ROOT@/etc/basemap-env.sh"

echo "=== verify  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
mkdir -p "@STATE@"

python3 - "@OUT@" "@SHARDS@" "@STATE@/xyz.done" <<'PY'
"""Refuse to let an incomplete tree be published.

A shard that rendered nothing is indistinguishable from a shard with nothing to
render, which is how a tree with no contours in any of its twenty-two million
tiles once shipped. The whole tree is sum(4**z) for z in 0..12 and every column
belongs to exactly one shard, so the total is an exact number, not an estimate.
"""
import json, pathlib, sqlite3, sys

out, shards, sentinel = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
MAX_ZOOM = 12
WHOLE = sum(4**z for z in range(MAX_ZOOM + 1))

manifest, total, seen = [], 0, set()
for index in range(shards):
    path = out / f"xyz.stage.{index:03d}of{shards:03d}.mbtiles"
    if not path.is_file():
        sys.exit(f"shard {index} of {shards} is missing: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit(f"{path.name} is corrupt")
    count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
    columns = {tuple(r) for r in db.execute(
        "SELECT DISTINCT zoom_level, tile_column FROM tiles")}
    db.close()
    if columns & seen:
        sys.exit(f"{path.name} shares columns with an earlier shard")
    seen |= columns
    total += count
    manifest.append({"shard": index, "file": path.name, "tiles": count,
                     "bytes": path.stat().st_size})
    print(f"    shard {index}: {count:,} tiles, {path.stat().st_size / 1e9:.1f} GB")

expected_columns = sum(1 << z for z in range(MAX_ZOOM + 1))
if total != WHOLE:
    sys.exit(f"the shards hold {total:,} tiles; the tree is {WHOLE:,} ({WHOLE - total:+,})")
if len(seen) != expected_columns:
    sys.exit(f"{len(seen):,} columns across the shards; the tree has {expected_columns:,}")

sentinel.write_text(json.dumps(
    {"shards": shards, "tiles": total, "max_zoom": MAX_ZOOM, "files": manifest}, indent=2))
print(f"    {total:,} tiles across {shards} shards, {len(seen):,} columns — complete")
PY

echo "=== done $(date -Is) ==="
