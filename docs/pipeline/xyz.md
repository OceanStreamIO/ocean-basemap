# 09 — Flat {z}/{x}/{y} Tile Tree

The basemap as plain tiles, for readers that will not take a PMTiles archive.

## Command

```bash
oceanbase run xyz
```

`BASEMAP_XYZ_STAGE_ONLY=1` stops after the render, before publishing.
`BASEMAP_XYZ_REDO_FROM=<z>` clears the resume marks at and below a zoom, for
when the reason a tile was missing has been fixed and "already done" is no
longer the truth.

## Output

```
basemap/xyz/{z}/{x}/{y}.webp        z0–12, opaque, no alpha
```

Past z12 the client stretches the last tile. Declare it and every library does
this for you: `maxNativeZoom: 12` in Leaflet, `maxzoom: 12` in MapLibre,
`maxZoom: 12` in OpenLayers, `maximumLevel: 12` in Cesium.

The tree is composited from the finished raster archives rather than rendered
again from the grids, so it cannot drift from what the PMTiles show — it is
made of them. Which archive supplies which half of a tile is a function of
zoom: `land_relief_eu` above z11 and `land_relief` below, `bathy_hr` above
z12, `bathy_eu` above z9, `bathy_global` below.

## Contours are baked in

A PMTiles viewer draws the depth contours from the vector archive and can turn
them off. A plain `{z}/{x}/{y}` consumer has no overlay and no toggle, so for
this tree the lines have to be in the pixels — which is what Esri and EMODnet
publish too.

`depth_contours.pmtiles` carries a curated set rather than a fixed interval:
200 m steps through the deep, the 500 m multiples in between, and a shallow
set for the shelf. Drawing all of it is a thicket. What goes into the tiles is:

| | Depths | Weight |
|---|---|---|
| Minor | every 500 m | alpha 64 |
| Major | every 1000 m | alpha 104 |
| Shelf | 10, 20, 30, 40, 50, 75, 100, 150, 200 m | alpha 64, **from z9 only** |

The shelf set is gated because without it a coastal tile at high zoom draws
nothing at all — measured over the Peru–Chile margin at z10, the whole shelf
came out blank — and because at low zoom those nine lines crowd every coast in
the world into a smear.

Both weights share one colour and differ only in alpha, so they go into one
coverage mask and come out in a single blend. Against separate colours per
weight the two differ by under ten in each channel, which is invisible under a
line one pixel wide, and it halves the work.

### Lines, not pixels

The contour archive stops at z10. Above it the **geometry** is re-projected
into each tile rather than the z10 image being stretched, so a contour is one
pixel wide at z12 exactly as it is at z10. Stretching would give a line four
pixels wide and soft.

### What it costs

Measured on sampled tiles, walking in tile order:

| Zoom | Tiles carrying a contour | Segments per tile | Draw |
|---|---|---|---|
| z8 | 67% | 41 | 2.0 ms |
| z10 | 61% | 9 | 1.7 ms |
| z12 | 49% | 3 | 1.8 ms |

z12 dominates the tile count, and there a tile costs about 1.4 ms including
the lookup. The geometry is decoded once per **source** tile and reused by all
sixteen of its z12 descendants, and every per-point step is numpy — a tile on
a busy margin carries seventeen thousand points, which in a Python
comprehension is most of the cost of the whole tile.

The fast path survives. An open-ocean tile that is opaque, has no land over it
**and no contour through it** still goes out as the archive's own bytes, with
no decode and no re-encode.

## Publishing is versioned

Sending 22 million objects takes about as long as rendering them. Written
straight into the live prefix, the public map spends most of a day as a mix of
old and new tiles with a seam walking across it as the upload works through the
columns.

So a build publishes under its own prefix and the CDN is pointed at it
afterwards:

```
s3://oceanstream-bucket/basemap/rev-<rev>/xyz/{z}/{x}/{y}.webp
./deploy/cdn/switch-xyz.py <rev>
```

`BASEMAP_XYZ_PREFIX` sets it; the default is the live `basemap/xyz`, so a local
run behaves as before. The switch is a CloudFront cache behaviour for `/xyz/*`
whose origin carries the revision in its `OriginPath`, so the public URL never
changes — CloudFront resolves a request as OriginPath + URI and does not strip
the path pattern, which is why the keys end in `/xyz/{z}/{x}/{y}.webp`.

The previous tree stays in the bucket, so a rollback is the same script with
the old revision. Deleting it is separate and deliberate: 22 million objects
takes hours, and it is the only thing standing between a bad switch and an
outage.

The per-column resume marks are keyed by prefix. Publishing the same staging
database to a new prefix has to send every column again, and believing the
marks from the last destination would silently publish a partial tree.

## Lakes and rivers

The first run of this tree had no lakes in it at all. The vector map draws
them from `lakes.pmtiles` over the relief; there is no vector layer here, so
every lake in the world came out as land — the Great Lakes, the Caspian,
Baikal, and every fjordside vatn in Norway.

They are now rasterised into the tile from the same FlatGeobuf bands and the
same ramp the viewer uses, so the two maps agree:

| Zoom | Lakes at least |
|---|---|
| z2–4 | 1000 km² |
| z5–6 | 100 km² |
| z7–8 | 10 km² |
| z9–10 | 1 km² |
| z11–12 | all of them |

Tiling every pond from z2 would bury the few hundred lakes legible there,
which is what those bands are for. A lake is one tone — the mean depth
HydroLAKES models for it, not bathymetry — so only its edge needs smoothing,
and it is drawn at 2× and averaged down. A lake at or below 5 m surface
elevation takes the *sea* ramp instead of the lake ramp, because it sits
against the sea and should read like it.

Rivers are Natural Earth centrelines, drawn from z4 and **under** the lakes,
so a river meeting a lake runs into it rather than across it.

**A missing lake layer is an error, not an empty map.** `Water` refuses to
start without the lakes directory, and refuses a zoom whose band file is not
on disk — checked once at the top of `run()` so it fails in a second rather
than after hours of rendering. The quiet version of this is what shipped a
lakeless tree the first time: a lake that is missing and a lake that is
genuinely not in this tile look identical in the output.

## Names are not in here

Nothing in this tree is text. Labels come from a layer stacked over it —
currently Esri's `World_Ocean_Reference`. Our own equivalent is built and
tested but [parked](reference.md).
