# 06 — Land Relief

Renders shaded land relief from the Copernicus DEM, coloured on
`color-ramps/land-emodnet.txt`. This is a curated palette inspired by the EMODnet
World Base Layer: muted sage lowlands, beige hills, then warmer uplands and rock.
`land-warm.txt` and the files under `color-ramps/sampled/` are historical references;
the renderer does not read them.

EMODnet uses a different combination of land elevation sources (see its
[World Base Layer description](https://emodnet.ec.europa.eu/en/bathymetry)), so this
is a visual approximation, not an exact reproduction. Median colours sampled from
its rendered tiles contain both terrain shading and land-cover differences; they
cannot uniquely recover an elevation-only colour ramp.

## Command

```bash
oceanbase run hypsometric
```

## Process

1. **Download** the Copernicus DEM: GLO-30 (30 m) inside the EMODnet extent, and GLO-90
   (90 m) everywhere else. That's about 26,500 files and 125 GB. The step projects the
   size first and stops if the disk can't hold it with a 30 GB margin. Files already
   present are skipped.
2. **Mosaic** each product with `gdalbuildvrt -resolution highest -r bilinear`, plus one
   mosaic per COG overview level. A mosaic of thousands of files has no overviews of its
   own, so low zooms read the coarsest level that is still finer than their pixels.
3. **Render**, with the same renderer as the [bathymetry](bathymetry.md) step.

## Land colour and shading

The Cabo Espichel comparison showed a pale, craggy land surface beside EMODnet's
softer sage/beige hills. Two parts of our settings could produce that appearance:

- The old 6 m shading reference exaggerated slopes about 2.0× at z12 and 2.7× at
  z11 near Cabo Espichel (about 30 m and 60 m per pixel). That also amplified
  fine Copernicus surface texture.
- The old highlight ceiling, 1.18, multiplied pale lowland channels above 216
  past 255, clipping away their colour. Changing only the ramp would still
  leave white highlights and strong shadows.

The screenshots do not identify the rendered revision. Check the deployed archive
or XYZ version during comparison: a code update alone does not change baked tiles.

`LAND_SHADING` in `pipeline/hypsometric.py` now uses a 20 m reference, strength
0.60, power 0.42, and light-factor bounds 0.76–1.08. Exaggeration is about 1.2×
at z12 and 1.6× at z11 at the cape. Flat ground retains its exact ramp colour;
slopes keep directional relief with less contrast. The lowland palette changes
only below 800 m; the higher stops and separate ice palette are retained.

The DEM, source priorities, tile resolution and cubic resampling are retained.
No blur is added: this changes how terrain is lit, not the terrain itself. The
shared shading algorithm and the bathymetry's separate settings are unchanged.
Keep the viewer's land raster opaque, since lowering opacity blends water colour
into land and does not affect the baked XYZ tiles.

These settings have synthetic regression coverage, but have **not** been visually
validated on the source DEM for this revision. Review the remote render before
promoting it, including flat land and deserts as well as the cape.

## Output

| Archive | Zooms | Coverage | Sources, best first |
|---------|-------|----------|---------------------|
| `land_relief.pmtiles` | z0–7 | land tiles | GEBCO |
|  | z8–10 | land tiles | GLO-30, GLO-90, GEBCO |
| `land_relief_eu.pmtiles` | z11–12 | EMODnet extent | GLO-30, GEBCO |

GLO-30's native pixel is about z12, so MapLibre overzooms it beyond that.

## Rebuilding an existing installation

Pull the new commit on the machine holding the source data. Existing archives
are skipped, and partially rendered archives resume from their staging databases;
rerunning `hypsometric` alone will not replace them. Preserve the previous output
for comparison and retire both the old archive and its `.stage.mbtiles` for
`land_relief` and `land_relief_eu` before rebuilding. The `globe` step inherits the
same land style, so also retire `land_geo.pmtiles`, `land_geo.stage.mbtiles` and any
`land_geo.stage.shift.mbtiles` before running it.

For the existing EDITO build Job, choose a **new, unpublished** XYZ version and run
on the deployment machine (the example name below must not already be in use):

```bash
CODE_REV="$(git rev-parse --short HEAD)" \
REBUILD="land_relief land_relief_eu land_geo" \
BUILD_STEPS="hypsometric globe xyz" \
XYZ_REDO_FROM=0 XYZ_VERSION=land-relief-review \
./deploy/edito/relief-build/run.sh
```

This uses the existing staged DEMs and sea archives. The job's final upload writes
PMTiles to `basemap-build/out/`; the XYZ version is separate from the live tree.
`XYZ_REDO_FROM=0` clears every flattened-tile resume mark, because the land style
affects every zoom. The job clears named relief archives once per `CODE_REV`, then
resumes on retry; use a new committed revision when changing the style again.
If a geographic finalize was interrupted, remove the obsolete
`land_geo.stage.shift.mbtiles` as well. For an HPC build, similarly replace the
land archives and rerender all XYZ shards from z0; staging old PMTiles or reusing
completed columns would keep the previous appearance.

Before promotion, compare at matching zoom and extent:

| View | Check |
|------|-------|
| Cabo Espichel / Serra da Arrábida, z10–12 | Sage/beige hills, readable cliffs, fewer white highlights |
| Po plain and Oslofjord, z10–12 | Small landforms remain visible; lowlands do not become flat washes |
| Alps, z7–12 | Ridges remain legible through the global/regional zoom transitions |
| Sahara and Morocco, z7–10 | Muted lowland tint does not imply green vegetation |
| Greenland / Antarctica, globe z0–6 | Ice stays on its own palette with visible large-scale relief |
| Coastlines and open ocean | Coastline alignment, depths, contours and water colours remain consistent |

Compare open-ocean tiles against the previous version as well: with the same sea
archives, they should be byte-identical. Mixed coast tiles are re-encoded with the
land, so small WebP differences in water pixels do not imply changed bathymetry.
Promote the land PMTiles and switch the XYZ version only after visual review.

## Ice

The land ramp is hypsometric: colour stands for height, and by 1,750 m it has left
green for the browns of bare rock. That is right for the Alps and wrong for an ice
sheet, whose surface is ice from the coast to the summit — Greenland's dome stands at
2,000–3,200 m and so came out the colour of desert, as did Antarctica.

Elevation alone cannot tell the two apart, and widening the land ramp towards white in
that band would bleach the Tibetan plateau and the Altiplano with it. So ice is masked
instead: Natural Earth's glaciated areas and Antarctic ice shelves are merged into
`work/ice/ice_10m.geojson`, and the pixels they cover take a separate ice ramp
(`color-ramps/ice.txt`). The blend follows the mask's coverage fraction, so the margin
grades rather than steps.

Ice is nearly flat beside rock — the middle of the Greenland sheet falls about a metre
per kilometre — so at the shading that suits terrain it renders as featureless white.
The hillshade contrast is therefore stretched about its flat tone over ice
(`ICE_RELIEF_GAIN`), which brings out the dome, the outlet glaciers and the flow
stripes. The light itself is computed once per tile, so nothing shifts at the ice edge
except the colour.

Natural Earth is exact over the sheets and generalised at their margins, which is where
a sharper mask would show; the ice edge is visibly polygonal by about z10.

The tiles carry no land mask. The viewer draws them under the vector sea mask, which
hides their sea part along the exact OSM coastline. The previous GEBCO relief rasterised
its mask on the 463 m grid, and that is what stepped over the shoreline.
