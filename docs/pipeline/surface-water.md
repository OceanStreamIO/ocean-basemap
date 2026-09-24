# 05d — Observed Surface Water

The mask that stops lagoons being land.

## Command

```bash
oceanbase run surface-water
```

Downloads 504 tiles (~15 GB), builds internal overviews, and writes the
mosaics the bathymetry reads.

## The problem

Every mask in this pipeline is cut from the OSM land polygons, and those are
built from the OSM **coastline** — which runs along the outer barrier islands.
Everything behind it falls *inside* the land: the Venetian lagoon, the Wadden
flats, a thousand estuaries and deltas. They are drawn as land, at every zoom.

HydroLAKES does not rescue them. It excludes water connected to the sea, so
around Venice it holds twenty-six fish-farm basins and not the lagoon itself.

**The depth is not missing.** EMODnet's DTM covers the Venice lagoon at 100%,
down to −40 m, and we already hold it. Only the mask hides it.

## Why elevation cannot be the test

The obvious fix is to call everything below 0 m water. Measured:

| | Land pixels below 0 m |
|---|---|
| Venice lagoon and its surroundings | 60% |
| Flevoland | **83%** |

It drowns the entire Dutch polder and leaves the villages as islands, and even
where it gets the lagoon right the coast comes out ragged. Land below sea level
is still land.

## What is used instead

The JRC **Global Surface Water** dataset: 30 m, global, built from 38 years of
Landsat, where each pixel carries the percentage of clear observations in which
it was water.

> Pekel, Cottam, Gorelick & Belward (2016), "High-resolution mapping of global
> surface water and its long-term changes", *Nature* **540**, 418–422.

A polder is dry in every scene and stays land. A lagoon is wet in every scene
and becomes water. The threshold is **80%**, which keeps tidal flats and the
lagoon while leaving seasonal paddies, salt pans and river bars as land. It
also picks up the large rivers and reservoirs, which is the other half of the
detail EMODnet has and we did not.

Outside the dataset's coverage — above 80°N, below 60°S — there are no tiles,
so the read comes back empty, the answer is "not water", and the poles are left
exactly as the coastline had them.

## Overviews

The tiles ship at 40,000 px a side with **no overviews**, so a z8 tile would
read a hundred megapixels of source for a 258 px window. Levels 2–32 are built
internally, taking 30 m down to about 960 m, which is coarser than a z8 pixel
anywhere. `Source.pyramid` then picks the coarsest stand-in still finer than
the tile being drawn.

The nodata value has to be declared before that. The files state none, and 255
means "never observed"; averaged in as a number it would drag a coast over the
threshold. Declared, GDAL leaves it out of the average.

## What reads it

`sea_alpha` in `processing/raster_tiles.py`, so all three bathymetry archives
gain the lagoons at once — and the flat tile tree with them, since it is
composited from those archives.

One consequence, handled in `pipeline/xyz.py`: the big lakes are observed water
too, so the bathymetry now covers them. On the sea's ramp a lake lands in the
near-white shallows and vanishes against the land, which is why lakes have a
ramp of their own. The flat tree therefore paints lakes **after** the sea
blend, so their own ramp wins wherever a lake polygon says there is a lake.
