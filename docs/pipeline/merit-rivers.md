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

Beside them sits `grun_ensemble_mmm_runoff_1990_2019.tif`, the runoff the discharge is estimated from (see [Discharge](#discharge)). It is made once from G-RUN ENSEMBLE's monthly NetCDF and is small enough to make anywhere:

```bash
oceanbase merit-runoff G-RUN_ENSEMBLE_MMM.nc grun_ensemble_mmm_runoff_1990_2019.tif
```

## Process

1. **Extract, per tile.** Every cell draining at least `BASEMAP_MERIT_MIN_UPA` km² (default 10) is kept, with the global index of the cell it flows into. The downstream index is arithmetic on the cell's own direction, wrapping at the antimeridian, so a tile needs nothing from its neighbours and the tiles run in parallel. Each tile's cells are saved, so an interrupted run resumes.
2. **Join, order, route and cut.** All cells are joined into one network. Strahler order comes from a topological pass, one level at a time: a source is 1, and a cell takes the highest order flowing into it, plus one where two or more arrivals share it. The discharge is routed in the same pass (see [Discharge](#discharge)). The network is then cut into stretches between confluences, each ending on the cell it flows into so that tributaries meet their river.
3. **Shape.** Each stretch is smoothed once, ends fixed, to round off the 90 m stair-steps of D8, and simplified to half a cell.
4. **Write** `work/land/merit_rivers.fgb`: one line per stretch, with a spatial index.

| Field | Meaning |
|---|---|
| `order` | Strahler order |
| `dis` | estimated long-term mean discharge at the stretch's downstream end, m³/s |
| `upa` | drainage area at the stretch's downstream end, km² |
| `width` | mean of MERIT's widths along the stretch where it has one, m |
| `wet` | share of the stretch's cells with a width |

The 10 km² threshold is where HydroRIVERS starts a river too, so the Strahler orders are comparable and the zoom rules written for HydroRIVERS mean roughly the same here.

## Discharge

HydroRIVERS carries a modelled long-term mean discharge, which the zoom rules use beside the order. MERIT carries none, and drainage area is a poor stand-in: in wet regions the two rise together, but a desert wadi can drain a large area and carry almost no water, and a short river on a wet coast carries a lot from a small one. A comparison against the current map showed both failures at once: selecting by area drew the Sahara's dry valleys and missed most of Kodiak's rivers.

So the step estimates a discharge, the same way HydroRIVERS' own was made: runoff routed down the network. The runoff is G-RUN ENSEMBLE (Ghiggi et al. 2021), a reconstruction of monthly runoff since 1901 at 0.5°, trained on gauged small basins; its ensemble mean over 1990–2019 is averaged into one grid of mean annual runoff in mm/yr. At half a degree a coast, a delta or a small island is often a sea cell, so the gaps are filled up to ten cells into the nearest land values.

In the topological pass that computes the order, each river cell adds the runoff of the land that drains straight into it: its own drainage area minus that of the river cells flowing into it, times the runoff where it lies. One km² under one mm a year is 1,000 m³ a year. It then passes everything on downstream, so each stretch ends with an estimated long-term mean discharge in m³/s.

This is a climatological flow that only ever grows downstream. Evaporation from rivers and wetlands, irrigation, and reservoirs are not in it, so it runs high below where they matter: the Nile below Aswan, the Niger's inland delta, irrigated basins. It is right for ranking rivers on a map. For flood work it is a start, not a substitute for a hydrological model such as GloFAS.

## Size and time

Measured on a synthetic network of 2 million cells: about one second to join, order and cut, and 225 bytes of memory a cell. The world at 10 km² is a few hundred million cells, so tens of gigabytes: one standard cluster node, not a laptop.

## Licence

MERIT Hydro is dual-licensed, CC BY-NC 4.0 or ODbL 1.0; this project uses it under ODbL 1.0. The traced network is a derived database and is published under ODbL 1.0. Maps and tiles drawn from it are produced works, licensed as the project decides, and credit it:

> Contains information from MERIT Hydro (Yamazaki et al. 2019, doi:10.1029/2019WR024873), made available under the ODbL 1.0

Yamazaki, D., Ikeshima, D., Sosa, J., Bates, P. D., Allen, G. H., & Pavelsky, T. M. (2019). MERIT Hydro: A high-resolution global hydrography map based on latest topography datasets. *Water Resources Research*, 55, 5053–5073. doi:10.1029/2019WR024873

G-RUN ENSEMBLE is under CC BY 4.0 ([figshare, doi:10.6084/m9.figshare.12794075](https://doi.org/10.6084/m9.figshare.12794075.v1)), and the discharge credits it:

> Discharge estimated from G-RUN ENSEMBLE runoff (Ghiggi et al. 2021, doi:10.1029/2020WR028787), CC BY 4.0

Ghiggi, G., Humphrey, V., Seneviratne, S. I., & Gudmundsson, L. (2021). G-RUN ENSEMBLE: A multi-forcing observation-based global runoff reanalysis. *Water Resources Research*, 57, e2020WR028787. doi:10.1029/2020WR028787

## Output

| File | Description |
|---|---|
| `work/merit_rivers/cells_<threshold>/*.npz` | the river cells of each tile, for resuming |
| `merit_hydro/grun_ensemble_mmm_runoff_1990_2019.tif` | the runoff grid, made by `oceanbase merit-runoff` |
| `work/land/merit_rivers.fgb` | the network, one line per stretch |
