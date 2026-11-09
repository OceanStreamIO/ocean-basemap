# 06 — Land Relief

Renders shaded land relief from the Copernicus DEM, coloured on `color-ramps/land-soft.txt` and tinted by land cover. The lowland is a pale warm grey that darkens and warms into the hills and climbs back to near-white for the high ranges; forest, pasture, cropland and wetland each shift it a little; the hillshade is strong but gated so that flat ground keeps its exact colour. `land-emodnet*.txt`, `land-warm.txt` and the files under `color-ramps/sampled/` are the earlier treatments and remain selectable with `BASEMAP_LAND_STYLE` for comparison.

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

The look was changed on 2026-11-09 to bring the land closer to EMODnet's base layer. What read as "rough" beside it was not fine detail, of which ours had less, but shadow: dark, grey and cut off flat at a 0.70 floor, with 31% of Alpine land pixels within reach of that limit. Four things changed together. All of them are `LAND_LOOK` in `pipeline/hypsometric.py`.

**The ramp** is `land-soft.txt`. To 1,800 m it is the median of EMODnet's own tiles over flat ground, measured against the DEM at seven sites; their land is a pure elevation ramp. Above that it leaves EMODnet on purpose, because their hard step to white at about 2,000 m blotches Utah and Colorado and their second terracotta band at 3,500–5,000 m bands Tibet. It climbs through stone and a tan plateau, reaching snow above 5,000 m. Under a deuteranopia simulation (Machado 2009) the blue-yellow axis, which is what a red-green reader has, spans about +1 to +29 against +4 to +10 for the previous ramp.

**The light** is `LAND_SHADING`: strength 0.90, a 20 m reference, power 0.38 and a 0.56–1.14 band, gated below 2° on the exaggerated surface so a plain keeps its ramp colour. It is `soft`: the factor eases into both limits instead of being cut at them, highlights lift toward white so no channel clips, and with `warm` 0.18 a shadow loses blue faster than red, so it deepens the colour under it instead of greying it.

**Each tile is rendered deeper and averaged down**: two zooms deeper to z6, one to z11, none at z12, where GLO-30's own pixel is about the tile's. Two things are deliberate. The exaggeration stays the tile's own, since rendering at the deeper level's would flatten the relief. And a deeper level reads its own sources, so z6 and z7 are averaged from z8 and come from the Copernicus DEM rather than GEBCO. It costs about 3.6 million tile renders against 1.3 million.

**The land cover tint and the shoreline features** are in `processing/landcover.py` and read the [WorldCover](worldcover.md) mosaic and the [surface water](surface-water.md) occurrence. Each pixel is shifted by its ESA WorldCover class at 0.40 strength: trees darker and bluer, crops lighter and warmer, wetland bluer. It was 0.67 until this change, where forest read as shadow. Tidal flats, wetland and sand follow at a quarter strength, as before.

Other looks stay selectable with `BASEMAP_LAND_STYLE`, on the datalab build and on the cluster:

| Style | What it is |
|---|---|
| `c` (default) | the above |
| `m` | the same light, tint and averaging on `land-soft-muted.txt`, two thirds of the way from the previous ramp to `land-soft.txt`. Not published; kept to try if the full colour turns out too strong |
| `muted` | what was published from 2026-10-15 through v13: `land-muted.txt`, a hard 0.70–1.18 light gated at 4°, tint 0.67, no averaging |
| `emodnet`, `l1`, `l3`, `l4` | older candidates |

The DEM, source priorities, tile resolution and cubic resampling did not change. The cover is read at its 74 m stand-in, nearest-neighbour, so at z12 a patch edge is two pixels square; averaging per class would soften that, and was not judged.

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
