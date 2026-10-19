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
# @EXPORTS@

echo "=== verify  job $SLURM_JOB_ID on $(hostname)  $(date -Is) ==="
mkdir -p "@STATE@"

python3 - "@OUT@" "@SHARDS@" "@STATE@/xyz.done" "@MAX_ZOOM@" <<'PY'
"""Refuse to let an incomplete tree be published.

A shard that rendered nothing is indistinguishable from a shard with nothing to
render, which is how a tree with no contours in any of its twenty-two million
tiles once shipped. The whole tree is sum(4**z) for z in 0..12 and every column
belongs to exactly one shard, so the total is an exact number, not an estimate.

The detail zooms past that (BASEMAP_XYZ_DETAIL_MAX_ZOOM) are exact too: they
are drawn where the HR archive has tiles and nowhere else, so their count is
read from the archive and held to the same standard.
"""
import json, os, pathlib, sqlite3, sys

out, shards, sentinel = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
MAX_ZOOM = int(sys.argv[4])
DETAIL = int(os.environ.get("BASEMAP_XYZ_DETAIL_MAX_ZOOM") or 0)
WHOLE = sum(4**z for z in range(MAX_ZOOM + 1))

manifest, total, deeper, seen = [], 0, 0, set()
for index in range(shards):
    path = out / f"xyz.stage.{index:03d}of{shards:03d}.mbtiles"
    if not path.is_file():
        sys.exit(f"shard {index} of {shards} is missing: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit(f"{path.name} is corrupt")
    count, detail = db.execute(
        "SELECT coalesce(sum(zoom_level <= ?), 0), coalesce(sum(zoom_level > ?), 0) "
        "FROM tiles", (MAX_ZOOM, MAX_ZOOM)).fetchone()
    columns = {tuple(r) for r in db.execute(
        "SELECT DISTINCT zoom_level, tile_column FROM tiles")}
    db.close()
    if columns & seen:
        sys.exit(f"{path.name} shares columns with an earlier shard")
    seen |= columns
    total += count
    deeper += detail
    # Per shard the publisher holds each file to this count, detail included.
    manifest.append({"shard": index, "file": path.name, "tiles": count + detail,
                     "bytes": path.stat().st_size})
    print(f"    shard {index}: {count + detail:,} tiles"
          + (f" ({detail:,} at the detail zooms)" if detail else "")
          + f", {path.stat().st_size / 1e9:.1f} GB")

expected_columns = sum(1 << z for z in range(MAX_ZOOM + 1))
columns = sum(1 for z, _ in seen if z <= MAX_ZOOM)
if total != WHOLE:
    sys.exit(f"the shards hold {total:,} tiles to z{MAX_ZOOM}; the tree is {WHOLE:,} ({WHOLE - total:+,})")
if columns != expected_columns:
    sys.exit(f"{columns:,} columns to z{MAX_ZOOM} across the shards; the tree has {expected_columns:,}")

if DETAIL > MAX_ZOOM:
    from basemap.config import get_data_dir, get_tile_dir
    from basemap.pipeline.xyz import detail_archive, detail_zooms

    archive = detail_archive(get_tile_dir(get_data_dir()))
    want = sum(len(archive.tiles_at(z)) for z in detail_zooms(MAX_ZOOM, DETAIL))
    if deeper != want:
        sys.exit(f"the shards hold {deeper:,} tiles at z{MAX_ZOOM + 1}-{DETAIL}; "
                 f"the HR archive has {want:,} there ({want - deeper:+,})")
elif deeper:
    sys.exit(f"the shards hold {deeper:,} tiles past z{MAX_ZOOM} and no detail zooms were asked for")

# "tiles" stays the first key: export-tiles.sh reads the first one it finds.
sentinel.write_text(json.dumps(
    {"shards": shards, "tiles": total + deeper, "max_zoom": MAX_ZOOM,
     "detail_max_zoom": max(DETAIL, MAX_ZOOM), "detail_tiles": deeper,
     "files": manifest}, indent=2))
print(f"    {total + deeper:,} tiles across {shards} shards, {len(seen):,} columns — complete"
      + (f", {deeper:,} of them at z{MAX_ZOOM + 1}-{DETAIL}" if deeper else ""))
PY

echo "=== done $(date -Is) ==="
