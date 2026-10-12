# 02d — NOAA BlueTopo

Stages NOAA's National Bathymetric Source as local grids for the HR bathymetry stack. BlueTopo is a 4–16 m compilation of US coastal waters, public domain, and the only source of its kind available outside Europe. The step is **opt-in** and is driven by `BASEMAP_BLUETOPO_REGION`:

```bash
BASEMAP_BLUETOPO_REGION=conus oceanbase run bluetopo
```

Regions are `conus`, `alaska` and `prusvi`. Nothing runs when the variable is unset.

## Process

1. **Index** — read the tile scheme GeoPackage from the bucket and select the tiles intersecting the region
2. **Filter** — read each tile's raster attribute table from its `.aux.xml` sidecar and decide which contributors to drop
3. **Stage** — read band 1 over `/vsicurl`, mask the dropped contributors, write a single-band COG
4. **Gate** — discard a grid whose surviving coverage falls below `MIN_COVERAGE`

Only band 1 lands on disk. A CONUS pull is 540 GB to 1.1 TB of three-band float32 at the source and under a fifth of that once staged.

## Input

| Source | Value |
|--------|-------|
| Bucket | `noaa-ocs-nationalbathymetry-pds` (AWS open data, anonymous) |
| Tile scheme | `BlueTopo/_BlueTopo_Tile_Scheme/` |
| Format | 3-band float32 COG — elevation, uncertainty, contributor |
| Vertical datum | NAVD88, orthometric, so it stacks on GEBCO with no chart-datum offset |
| Licence | US public domain |

## The contributor table decides everything

Band 3 indexes a raster attribute table, one row per contributing survey. Three of its columns carry the whole decision:

| Column | Meaning |
|--------|---------|
| `data_assessment` | 1 = survey, 3 = fill |
| `survey_date_start` | when the contributing survey was made |
| `source_survey_id` | the survey's own identifier |

The step keeps `data_assessment == 1` and `survey_date_start >= MIN_SURVEY_YEAR` (1960). Both halves are needed, and neither is sufficient.

**The grade alone is not enough.** A 1935 lead-line survey grades 1, the same as 2013 multibeam, because the grade is about vertical accuracy and those soundings are accurate. What they are not is *dense* — hundreds of metres apart, gridded onto 4 m — and the gridding shows as speckle and straight track lines across the seabed. Measured over 60 random CONUS tiles, 11.4 % of pixels are pre-1960 against 39.5 % from 1990 or later.

**The date alone is not enough either.** Gaps deeper than 200 m are filled from a generalised compilation that grades 3 and carries no useful date; rendered at z12 and beyond it shows visible triangular facets.

## Why Monterey Bay does not improve, and what that means

Esri's Ocean Basemap shows the full Monterey Canyon system in detail. BlueTopo as staged here does not, and the reason is worth recording because it is counter-intuitive and the measurement is not cheap to repeat.

Over the 46 tiles covering the bay:

| Grade | Era | Share | Median roughness | What it is |
|---|---|---|---|---|
| 1 | 1930s | 46.2 % | 0.005 | `H0xxxx.interpolate` — lead-line surveys, interpolated; **planar** |
| 3 | undated | 44.9 % | 0.186 | `NBS Generalization` — **this is the canyon** |
| 1 | 1960+ | 8.9 % | 0.2–0.5 | modern lidar and multibeam, a thin coastal ribbon |

The published filter keeps the last row alone, which is 6 % of the view. So the detail Esri shows *is* inside BlueTopo — but it sits in the grade-3 layer, and the grade-1 data that the filter trusts is the flat one. The rule is upside-down here while being right almost everywhere else.

Taking grade 3 wholesale is not the answer: it brings the canyon and the deep-water facets together, and the facets are worse than the smooth GEBCO surface they would replace. Four rules were tried and all four fail:

- **grade only** — keeps the planar 1930s interpolations, loses the canyon
- **grade + date** (published) — 6 % coverage, a hard-edged ribbon
- **per-pixel texture gate** — punches holes through real terrain; unusable
- **per-contributor roughness** — leaves rectangular grid-shaped edges

Nothing in the attribute table expresses "this fill is derived from dense multibeam", which is the thing that actually separates the canyon from the facets.

The better data for this coast exists and is not in BlueTopo. The **California Seafloor Mapping Program** publishes 2 m grids built from CSUMB and MBARI multibeam with USACE lidar, surveyed 1998–2012 and released open by USGS. BlueTopo excludes it because BlueTopo is a *charting* compilation — NOAA and USACE hydrographic surveys for navigation — and research multibeam is not part of that remit. Adding CSMP as its own HR source is the route to matching Esri here; tightening the BlueTopo filter is not.

## Coverage floor

Coverage after masking is bimodal rather than even: across the 6,237 CONUS tiles, 36 % are under a quarter covered and 39 % over three quarters. A grid kept regardless would draw a ribbon of 4 m detail with a hard edge across an otherwise 460 m view, which reads as broken rather than detailed. `MIN_COVERAGE` (0.6) keeps a grid only when it can carry its whole area; below it GEBCO carries it, cleanly.

A grid that stops qualifying is **unlinked**, not merely skipped. Leaving the previous file on disk left 6,237 files where 2,873 qualified, and the stale ones kept rendering.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `BASEMAP_BLUETOPO_REGION` | *unset* | `conus`, `alaska` or `prusvi`; nothing runs when unset |
| `BASEMAP_BLUETOPO_WORKERS` | 12 | parallel tile pulls |
| `BASEMAP_BLUETOPO_MIN_SURVEY_YEAR` | 1960 | drop contributors older than this |
| `BASEMAP_BLUETOPO_MIN_COVERAGE` | 0.6 | drop a grid below this surviving fraction |

Reading the bucket needs `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`; without it GDAL lists the whole prefix on every open, which over 6,000 tiles is most of the time spent.

## Output

`work/bluetopo/grids/*.tif` — single-band COGs, consumed by `bluetopo_sources()` in the bathymetry step and composited into `bathy_hr` ahead of EMODnet and GEBCO.
