# 06 — Land Relief

Renders shaded land relief from the Copernicus DEM, coloured on `color-ramps/land-muted.txt` and tinted by land cover. The lowland is a pale warm grey that darkens and warms into the hills and climbs back to near-white for the high ranges; forest, pasture, cropland and wetland each shift it a little; the hillshade is strong but gated so that flat ground keeps its exact colour. `land-emodnet*.txt`, `land-warm.txt` and the files under `color-ramps/sampled/` are the earlier treatments and remain selectable with `BASEMAP_LAND_STYLE` for comparison.

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

Three things changed together on 2026-10-15, after the lagoon sites were judged against EMODnet's tiles: the Camargue, the Venice lagoon and Ria Formosa, then the Danube delta, the inner Oslofjord and the IJsselmeer. Measured on the pixels, the sea and the land colours already matched EMODnet's; what their plains had and ours lacked was variation, and what their coasts had was lagoons drawn as distinct bodies. Each of the three below was judged on its own first and found short, which is why they ship as one treatment.

**The ramp** is `land-muted.txt`: 65% of the way from a warm family to a neutral grey one, judged as a pair. Elevation reads on luminance and on the blue-yellow axis, which is what a red-green reader has. The two lowest stops are capped at 216 so that the light's 1.18 ceiling never clips a lowland slope; above 4,000 m the stops run to 250 and clip under full light, which on snow is what snow looks like.

**The light** is `LAND_SHADING` in `pipeline/hypsometric.py`: strength 0.95, a 20 m reference, power 0.42 and a 0.70–1.18 band, gated by slope. Below 4° on the exaggerated surface the light fades back to flat, so a plain keeps its ramp colour and only relief is lit. Without the gate that strength drew the Po plain as grey mottle from the surface model's own texture; without the strength the hills were flat beside EMODnet's. The gate is measured on the exaggerated slope rather than the real one so that it means the same thing at every zoom, the way the exaggeration itself does.

**The land cover tint and the shoreline features** are in `processing/landcover.py` and read the [WorldCover](worldcover.md) mosaic and the [surface water](surface-water.md) occurrence. Each pixel is shifted by its ESA WorldCover class at two thirds strength: trees darker and bluer, crops lighter and warmer, wetland bluer still. Built-up land is neutral; at half strength it drew Padova, Mestre and Faro as grey smudges on the plain, and at a tenth it still did. Then three features at a quarter strength: ground that Global Surface Water saw wet in 25–65% of its observations — tidal flats, salt pans, seasonally flooded fields — moves toward a grey-blue; wetland and mangrove go bluer and a shade darker; bare ground, which on a coast is sand, warmer and a shade lighter. The flats are the one that matters: without them Ria Formosa is a few channels in a sandy shore, with them it is a lagoon behind its barrier islands. Both inputs are required — a land archive rendered without them looks finished and is not the one that was judged — and `BASEMAP_LAND_COVER=off` renders plain land on purpose.

The DEM, source priorities, tile resolution and cubic resampling are unchanged. The cover is read at its 74 m stand-in, nearest-neighbour, so at z12 a patch edge is two pixels square; averaging per class would soften that, and was not judged.

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
