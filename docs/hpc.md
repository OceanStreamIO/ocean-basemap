# Running the build on an HPC allocation

A full rebuild is dominated by two things, and for a long time both were pinned
to one machine in the datalab:

| | Before | After |
|---|---|---|
| Flat-tile render, 22,369,621 tiles | 7–8 h on 14 workers | ~20 min across four nodes |
| Publishing those tiles | ~15 h | ~3 h |

The render moved because it is embarrassingly parallel and needs almost nothing
staged — the archives it composites come to about 31 GB. The publish got faster
for a different reason, described below, and it is now the larger of the two by
an order of magnitude.

The existing `basemap-build` Job is untouched and remains the fallback. Nothing
here replaces it; a step that misbehaves on the cluster can simply keep running
where it did.

## Why publishing was slow

One Python process sending objects saturates at about **400 PUT/s** regardless
of how many threads it is given. Measured against the object store from the
build pod:

| Threads | Rate | CPU |
|---|---|---|
| 1 | 43/s | 0.07 cores |
| 16 | 218/s | 0.48 cores |
| 64 | **413/s** | 1.13 cores |
| 128 | 394/s | 1.37 cores |

The ceiling is the interpreter, not the network — a keep-alive round trip is
10 ms, so 64 threads should manage thousands a second — and 128 threads are
*slower* than 64 while burning more CPU. Eight processes reach about 1,930/s.

So the publisher is parallel by process, one column of tiles per unit of work.
The parent keeps the resume marks and hands out coordinates; each worker opens
the staging database itself and sends what it reads. Nothing but integers crosses
the process boundary, which matters when the tiles are 31 GB.

`--processes` is the knob that matters. It ships at 8, inside one ordinary node.

## The shape of it

```
download, hillshade  ──►  object storage ──┐
                                           ├──► basemap-hpc (Job) ══ssh══► cluster scratch
public elevation bucket ───────────────────┘                                    │
                                                                                ▼
                        vector-a ─┐
                                  ├─► raster ─► xyz[0..N] ─► verify ─► sentinel
                        vector-b ─┘                                             │
                                                                                ▼
object storage  ◄── basemap-hpc (Job) ◄══ssh══ shards and archives on scratch
        │
basemap-xyz-publish (Job)  ──►  basemap/v<N>/xyz/{z}/{x}/{y}.webp  ─►  ./deploy/cdn/switch-xyz.py v<N>
```

Two Jobs, both small, both on ordinary nodes. **The cluster never connects out.** The site does not allow its machines to open connections to external hosts, so every transfer is opened from the datalab side: the driver reads from object storage and streams over SSH to the transfer node, and reads the shards back over SSH and uploads them. Nothing lands on the driver's disk; `deploy/hpc/xfer.py` does the moving, several streams at once, each file written under `.part` and renamed only at its full size, each upload checked by size. Measured: 96 MB/s in over four streams, about 22 MB/s per stream out, so the 50–60 GB each way of a full run is a quarter to half an hour.

Until October 2026 the transfers ran on the site's queued transfer suite instead, which reached object storage from some of its nodes. That was an accident of those nodes' network rather than a service: it worked on two nodes and never on the others, stopped after a maintenance, and the site has since confirmed that access to external hosts is not permitted from its transfer or compute machines. Nothing here uses that suite any more.

The export is deliberately not part of the scheduler's dependency chain: it runs on the datalab side, which no job on the cluster can depend on. The verify job writes a sentinel containing the shard manifest, and the driver comes back for it — which is what makes the driver three re-runnable phases rather than one script that cannot be resumed.

## Running it

Site values — account, hosts, filesystem paths, module names — are **not in this
repository**. Fill them in once:

```bash
cp deploy/hpc/site.env.example deploy/hpc/site.env    # gitignored
kubectl -n project-oceanstream create configmap basemap-hpc-site \
    --from-env-file=deploy/hpc/site.env
```

Then, once per allocation:

```bash
./deploy/hpc/wheelhouse.sh      # build the wheels for the cluster's interpreter
./deploy/hpc/sync-code.sh       # send the source
./deploy/hpc/bootstrap.sh       # directories, GDAL wrappers, tippecanoe
./deploy/hpc/bootstrap.sh --verify
```

And per build:

```bash
./deploy/hpc/run.sh                       # stage, submit, export
./deploy/hpc/status.sh --watch
RUN_ID=<id> XYZ_VERSION=v3 ./deploy/edito/xyz-publish/run.sh
./deploy/hpc/parity.py --prefix basemap/v3/xyz --sample 2000
./deploy/cdn/switch-xyz.py --dry-run v3 && ./deploy/cdn/switch-xyz.py v3
```

`PHASE=stage|submit|export` runs one phase. Everything is idempotent: staging
copies rather than syncs, submission adopts a queued job of the same name instead
of submitting a second one, and the publish resumes per column.

### Rebuilding part of a version

Most versions change a few archives, not all of them, and the rest are staged from the published map. `HPC_REBUILD` names the ones that must not be: they are cleared on scratch once per run and left out of that pull, so the render builds them, or the inputs bring them when the datalab has already built one whose sources the cluster does not have. `XYZ_FROM=raster` with `RASTER_STEPS` limits the render to what changed, and `BASEMAP_BATHY_ARCHIVES`, `BASEMAP_LAKE_ARCHIVES` and `BASEMAP_XYZ_DETAIL_MAX_ZOOM` are carried into the batch scripts as the datalab Job carries them into its steps. The detail zooms are held to an exact count like the rest of the tree: the verify job reads it from the HR archive.

A run made while a datalab build of the same version is still going sends its archives to a prefix of its own with `HPC_ARCHIVES_PREFIX`; by default both write to `basemap-build/out/tiles/` under the same names.

The sources a datalab build fetched for itself (the GMRT, BlueTopo, CUDEM and AusBathyTopo grids) reach the cluster the same way as the rest: copied into `basemap-build/inputs/` first, then staged.

### The river network

The river network traced from MERIT Hydro is its own job, independent of the render chain: `PHASE=sync,rivers ./deploy/hpc/run.sh`, or `./deploy/hpc/rivers.sh` by hand. It stages the MERIT packages from `basemap-build/merit_hydro/` to scratch, runs one whole-node job (`sbatch/70-merit-rivers.sbatch`) that traces the network and then runs the rivers step on it, and sends three things back: `merit_rivers.fgb` to `basemap-build/out/rivers/`, the staged `rivers.fgb` the flat tree reads to `basemap-build/inputs/gebco2026/work/land/`, and `rivers.pmtiles` to `basemap-build/out/tiles/`, from where `promote.sh rivers` publishes it. A network already traced with the current fields is kept, so a rerun after a change to the zoom rule costs only the rivers step. The packages have to reach object storage first, from the datalab side, because the cluster may not connect out. See [05h — River network from MERIT Hydro](pipeline/merit-rivers.md).

## Checking the result

Byte-for-byte comparison against the published tree is possible, and is the
strongest check available, because most of the ocean is a tile the renderer
passes through untouched — an opaque sea tile with no land over it and no contour
through it goes out as the archive's own bytes, with no decode and no arithmetic.
Those must be *identical*. When they are not, the inputs differ rather than the
arithmetic, which is exactly how a stale archive gets caught.

Composited tiles need a tolerance, and picking it needs one more fact: the tile
format is lossy. A single unit of difference in the array *before* encoding comes
back as up to twenty after it. So "no channel moves by more than one" fails on
precisely the noise it was meant to absorb. What separates noise from a real
change is its shape: a different instruction set perturbs pixels thinly and
everywhere, keeping the mean near zero, while a changed contour or a missing lake
puts a large difference in a small area. `parity.py` measures both.

```bash
./deploy/hpc/parity.py --shard /path/to/xyz.stage.000of004.mbtiles
./deploy/hpc/parity.py --prefix basemap/v3/xyz --sample 2000 --zooms 6,8,10,11,12
```

## What runs where, and why

| Step | Where | Why |
|---|---|---|
| `download` | datalab | needs the internet |
| every transfer | datalab, over SSH | the cluster may not connect out |
| `hillshade` | datalab | its `gdal2tiles` wants the module's own interpreter *and* numpy, the riskiest of the wrappers, and the flat tree does not read its output |
| vector steps | cluster | not faster there — `tippecanoe` is largely one process per archive — but they belong with the data |
| raster steps | cluster | the one group that wants a high-memory node |
| `xyz` | cluster | all the parallel work, and only ~31 GB of input |
| publish | datalab | needs the internet, and 22 million small files on a clustered filesystem is the worst possible shape for one |

## Things that were not obvious

**Ten steps reach the network inside `run()`**, not just `download`. Two of them
were fatal on a machine with no route out: the elevation tile list had no cache,
so the land relief died on its first call however much of the elevation data was
already on disk; and the download helper issued a HEAD for every file without a
checksum — which is every file but the main grid — at a 300-second timeout each.
Where packets are dropped rather than refused, that is five hours of waiting
before the surface-water step does any work. `BASEMAP_OFFLINE=1` makes a file
that is present the answer and a file that is absent an error in the first
second, naming itself.

**GDAL is two different GDALs.** The pipeline's own is inside its `rasterio` and
`fiona` wheels; the command-line tools come from a module, at a different
version. The module exports `GDAL_DATA` and `PROJ_LIB`, and those are read by
whichever GDAL is loaded — so leaving them set hands the wheel's GDAL another
version's projection database and changes coordinate handling with no error at
all. The batch environment unsets them and generated wrappers restore them for
the tools that need them.

**The tools' Python is not the pipeline's Python**, and the two have
contradictory requirements: the tools need the module's `site-packages` on
`PYTHONPATH`, and the virtual environment needs `PYTHONPATH` unset or it resolves
against the module's global packages. The wrappers invoke the tools' interpreter
explicitly rather than trusting their shebang.

**GDAL's block cache defaults to a share of total RAM** — gigabytes per process
on a large node. Left alone, a hundred render workers do not fit.

**Staging databases are written on node-local disk.** They are opened
write-ahead, and write-ahead logging needs a shared-memory mapping that a
network filesystem does not provide. The finished file is copied to shared
storage and its integrity checked after the copy.

**The elevation cells pass through the datalab like everything else.** They come from a public bucket, but the cluster may not fetch them itself, so the 135 GB are streamed through the driver once and kept on scratch between builds.

## Guards against a tree that looks fine

A shard that rendered nothing is indistinguishable from a shard with nothing to
render. That is not hypothetical — the first flat tree shipped with no contours
in any of its 22 million tiles and nothing said so. So the count is checked four
times, and it is an exact number rather than an estimate, because the tree is
`sum(4**z)` for z in 0..12 and every column belongs to exactly one shard:

1. each shard asserts its own total when it finishes rendering;
2. the verify job re-checks every shard, their sum, and that no two overlap;
3. the publisher refuses a shard whose count does not match the manifest, which
   catches a truncated download;
4. the publisher will not report success unless every column is marked sent.
