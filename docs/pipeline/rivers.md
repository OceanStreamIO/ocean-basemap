# 05g — Rivers (MERIT Hydro)

Stages the rivers the map draws from the MERIT network ([05h](merit-rivers.md)) and builds `rivers.pmtiles` from them. The flat tree draws its rivers from the staged file, the viewer from the archive, and both choose their stretches with the one rule in `SELECTION`, so the two maps show the same rivers.

## Command

```bash
oceanbase run rivers
```

It runs on the cluster in the same job that traces the network, because the network is 2.8 GB and already there: `PHASE=sync,rivers ./deploy/hpc/run.sh`. The staged file comes back to the build inputs and the archive to `basemap-build/out/tiles/`, from where `promote.sh rivers` publishes it.

## Process

1. **Read** `work/land/merit_rivers.fgb`, the traced network: 7.3 million stretches between confluences, each with a Strahler order and an estimated mean discharge. When it is missing, the step downloads the published one (`PUBLISHED_NETWORK` in `merit_rivers.py`, 2.8 GB) under a `.part` name and renames it when whole; offline, or when the file is older than the fields it needs, it stops.
2. **Stage** the stretches the deepest zoom draws — order 4 or more, or 10 m³/s or more — as `work/land/rivers.fgb` with two attributes, `order` and `dis`, and a spatial index. The tree's `Water` class queries it by bbox per block of tiles.
3. **Archive.** One tippecanoe band per zoom with its own rule, joined into `rivers.pmtiles` (z4–10; the viewer overzooms beyond).

## Which stretches, at which zoom

A stretch is kept when its order **or** its discharge clears the bar. Order alone misses the short, full rivers of a wet coast; discharge alone drops the dry channels of a desert.

| Up to | Order ≥ | or discharge ≥ |
|---|---|---|
| z5 | 7 | 500 m³/s |
| z6 | 6 | 200 |
| z7 | 6 | 100 |
| z8 | 5 | 50 |
| z9 | 5 | 20 |
| z10 and beyond | 4 | 10 |

The orders are the ones HydroRIVERS was drawn with, and the discharge bars are half of HydroRIVERS'. Both networks were measured worldwide (`oceanbase river-lengths`, then `river-compare`; the Job is `deploy/edito/river-lengths/`), by length of line per order, discharge band and 10° cell:

- With HydroRIVERS' rule unchanged, MERIT draws 109–121 % of HydroRIVERS' length worldwide, and 93–138 % in every region but Australia. The orders agree at the scale of a continent.
- They differ locally, on wet coasts above all, where short rivers carry a lot of water from small basins: on Kodiak the unchanged rule kept 2 of the 6 rivers HydroRIVERS draws. That is what the discharge bar is for. At half HydroRIVERS' bars Kodiak keeps 5, and MERIT draws 109–153 % of HydroRIVERS' length region by region.
- A quarter of HydroRIVERS' bars drew up to twice HydroRIVERS' length, the same everywhere: denser, not better balanced.

The pen follows the stretch too: two pixels for order 7 or 150 m³/s, 1.4 for order 5 or 50, a hairline otherwise.

## Dry channels

A stretch carrying under 0.5 m³/s (`DRY_DIS`) is dry for most of the year: a wadi, an ephemeral creek. It is still drawn when its order clears the bar, but as a dashed hairline, in the tree and the viewer alike. It is told from a river by its pattern, not its colour. They are kept rather than dropped because a flood map needs to know where they run: a wadi is where a flash flood goes.

The estimated discharge has no losses (see [05h — Discharge](merit-rivers.md#discharge)), so a river that dries out downstream, such as the lower Nile below Aswan, stays wet on the map. That is the right way round for a map, and the reason order alone was never the rule.

## Why not HydroRIVERS, or Natural Earth

The rivers were Natural Earth's centrelines, a few hundred of the world's largest, chosen for a wall map. At z10 that drew the Rhône through the Camargue and nothing else: no Vidourle, no Petit Rhône, none of the drainage that gives a delta its shape on EMODnet's map. `land_base.pmtiles` still carries the Natural Earth layer; nothing draws it.

HydroRIVERS replaced them until 2026-10. Its licence (HydroSHEDS v1, WWF) lets its data reach users only under terms at least as protective as WWF's, which rules out releasing the tiles under CC BY 4.0. MERIT Hydro is available under ODbL 1.0, which allows that.

## Source

| | |
|---|---|
| **Network** | traced from MERIT Hydro v1.0.1 (Yamazaki et al. 2019, doi:10.1029/2019WR024873), see [05h](merit-rivers.md) |
| **Discharge** | estimated from G-RUN ENSEMBLE runoff (Ghiggi et al. 2021, doi:10.1029/2020WR028787) |
| **Licence** | the network is a derived database under ODbL 1.0; the runoff is CC BY 4.0 |
