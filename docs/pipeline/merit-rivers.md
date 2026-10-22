# 05h — River network from MERIT Hydro

Traces a global river network from MERIT Hydro, to replace HydroRIVERS. HydroRIVERS is under the HydroSHEDS v1 licence, which does not let derived tiles go out under CC BY 4.0; MERIT Hydro offers ODbL 1.0, which does, and which also makes the traced network an open dataset of its own.

## Command

```bash
oceanbase run merit-rivers
```

On the cluster, through the driver Job: `PHASE=sync,rivers ./deploy/hpc/run.sh`, or by hand with `./deploy/hpc/rivers.sh` (stage, submit and wait, export).

## Input

MERIT Hydro v1.0.1 (Yamazaki et al. 2019): 3 arc-second grids, about 90 m, over all land from 90°N to 60°S, in 5° tiles packed into 30° tar packages. Three grids are read:

| Grid | What it is | Used for |
|---|---|---|
| `dir` | D8 flow direction | which cell each cell drains into |
| `upa` | upstream drainage area, km² | which cells are rivers, and how big |
| `wth` | river channel width, m | whether a channel holds visible water |

The packages are read in place through GDAL's `/vsitar/`, from `$BASEMAP_MERIT_DIR` or `work/merit_hydro/`. Downloading them requires registering on the [MERIT Hydro page](https://global-hydrodynamics.github.io/MERIT_Hydro/), which includes accepting the licence; the cluster gets them from object storage under `basemap-build/merit_hydro/`, because it may not connect out.

## Process

1. **Extract, per tile.** Every cell draining at least `BASEMAP_MERIT_MIN_UPA` km² (default 10) is kept, with the global index of the cell it flows into. The downstream index is arithmetic on the cell's own direction, wrapping at the antimeridian, so a tile needs nothing from its neighbours and the tiles run in parallel. Each tile's cells are saved, so an interrupted run resumes.
2. **Join, order and cut.** All cells are joined into one network. Strahler order comes from a topological pass, one level at a time: a source is 1, and a cell takes the highest order flowing into it, plus one where two or more arrivals share it. The network is then cut into stretches between confluences, each ending on the cell it flows into so that tributaries meet their river.
3. **Shape.** Each stretch is smoothed once, ends fixed, to round off the 90 m stair-steps of D8, and simplified to half a cell.
4. **Write** `work/land/merit_rivers.fgb`: one line per stretch, with a spatial index.

| Field | Meaning |
|---|---|
| `order` | Strahler order |
| `upa` | drainage area at the stretch's downstream end, km² |
| `width` | mean of MERIT's widths along the stretch where it has one, m |
| `wet` | share of the stretch's cells with a width |

The 10 km² threshold is where HydroRIVERS starts a river too, so the Strahler orders are comparable and the zoom rules written for HydroRIVERS mean roughly the same here.

## No discharge

HydroRIVERS carries a modelled long-term mean discharge, which the zoom rules use beside the order. MERIT carries none. Drainage area stands in for it, and in wet regions the two rise together. In dry ones they part: a desert wadi can drain a large area and carry almost no water. `width` and `wet` are there to drop such channels, and the thresholds are set by comparison with the current map before the switch. Discharge can be attached later from the Copernicus GloFAS reanalysis, by matching drainage areas.

## Size and time

Measured on a synthetic network of 2 million cells: about one second to join, order and cut, and 225 bytes of memory a cell. The world at 10 km² is a few hundred million cells, so tens of gigabytes: one standard cluster node, not a laptop.

## Licence

MERIT Hydro is dual-licensed, CC BY-NC 4.0 or ODbL 1.0; this project uses it under ODbL 1.0. The traced network is a derived database and is published under ODbL 1.0. Maps and tiles drawn from it are produced works, licensed as the project decides, and credit it:

> Contains information from MERIT Hydro (Yamazaki et al. 2019, doi:10.1029/2019WR024873), made available under the ODbL 1.0

Yamazaki, D., Ikeshima, D., Sosa, J., Bates, P. D., Allen, G. H., & Pavelsky, T. M. (2019). MERIT Hydro: A high-resolution global hydrography map based on latest topography datasets. *Water Resources Research*, 55, 5053–5073. doi:10.1029/2019WR024873

## Output

| File | Description |
|---|---|
| `work/merit_rivers/cells_<threshold>/*.npz` | the river cells of each tile, for resuming |
| `work/land/merit_rivers.fgb` | the network, one line per stretch |
