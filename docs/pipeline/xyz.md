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

`depth_contours.pmtiles` carries a curated set rather than a fixed interval: 200 m steps through the deep, the 500 m multiples in between, and a shallow set for the shelf. What goes into the tiles is the viewer's own scheme — `CONTOUR_CLASSES` in `xyz.py` mirrors the four line layers in `app.js` stop for stop, and a test holds the two together:

| Class | Depths | From | Width | Opacity | Dash |
|---|---|---|---|---|---|
| Major | every 1000 m | z2 | 0.4 → 1.6 px | 0.20 → 0.50 | solid |
| Minor | every 500 m not 1000 | z4 | 0.3 → 1.0 px | 0.12 → 0.42 | 6/3 |
| Shelf | 200 m steps within 2000 m, plus 50 and 100 m | z5 | 0.2 → 1.0 px | 0.10 → 0.40 | 3/3 |
| Shallow | 10, 20, 30, 40, 75, 150 m — and 50 and 100 again | z6 | 0.2 → 0.8 px | 0.10 → 0.30 | 2/2 |

Width and opacity interpolate linearly in zoom between the viewer's stops and hold flat past the last one, as MapLibre does. A dashed line is baked solid at the fraction of its period that is ink, so it carries the same weight without the dashes. A line in two classes — 50 and 100 m are named by the shelf layer and admitted by the shallow layer's filter — gets the coverage the two reach together.

This replaced a scheme that drew every 500 m multiple at one fixed weight from z0 up. At z3 that put forty-five thousand lines through one Pacific tile, which the viewer draws as a few faint thousand-metre lines, and in the Oslofjord at z11 the shallow steps came out solid and stacked where the viewer draws them dashed at a third of the weight. The tree is asked to look like the viewer, so the viewer's layers are the reference.

The pen is rounded to whole pixels of the supersampled mask — a 0.4 px line is drawn with a one-pixel pen at 2× — and the coverage is scaled the other way, so pen × coverage is always the viewer's width × opacity × dash fraction. Every class shares one tone and goes into one coverage mask, which comes out in a single blend.

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

So a build publishes to a **version** and the CDN is pointed at it afterwards:

```
s3://oceanstream-bucket/basemap/v2/xyz/{z}/{x}/{y}.webp
./deploy/cdn/switch-xyz.py v2
```

`XYZ_VERSION` on the build sets it; unset, the tree goes to the live
`basemap/xyz` exactly as before.

A version, not a build revision. Versions are few and are meant to be pinned
to; one prefix per build would leave 22 million objects to clean up every time
and pin to nothing worth pinning to. A rebuild that only fixes something
republishes the same version; one that changes how the map looks earns the
next.

| URL | Serves |
|---|---|
| `/xyz/{z}/{x}/{y}.webp` | whatever is current |
| `/v2/xyz/{z}/{x}/{y}.webp` | that version, for as long as it exists |

The second form needs no CDN configuration — the default origin already points
at `basemap/`, so a version is reachable the moment it is published. Consumers
who cannot afford the map changing under them pin it, and switching what
`/xyz/` serves does not disturb them.

The switch itself is a cache behaviour for `/xyz/*` whose origin carries the
version in its `OriginPath`. CloudFront resolves a request as OriginPath + URI
and does not strip the path pattern, which is why the keys end in
`/xyz/{z}/{x}/{y}.webp` rather than at the version.

Rolling back is the same script with the previous version, or `--default` to
fall back to the unversioned tree. Deleting an old version is separate and
deliberate: 22 million objects takes hours, and it is the only thing standing
between a bad switch and an outage.

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
