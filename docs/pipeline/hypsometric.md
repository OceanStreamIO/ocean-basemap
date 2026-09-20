# 06 — Land Relief

Renders shaded land relief from the Copernicus DEM, coloured on
`color-ramps/land-warm.txt`, a ramp derived from the EMODnet World Base Layer sample.

The sampled ramp itself (`land-emodnet.txt`) is faithful and almost unreadable close
up: sea level to 350 m spans 20 levels of grey-green, so a whole fjord landscape comes
out as one wash. No viewer-side correction helps, because that range sits in the top
fifth of the scale where `raster-contrast` clips to white and *shrinks* the spread. The
shipped ramp darkens the lowlands and opens the spread to about 77 levels, keeping
EMODnet's hues and its exact colours above 2,500 m. Its low stops are khaki rather than
green so that deserts do not read as vegetation.

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

## Output

| Archive | Zooms | Coverage | Sources, best first |
|---------|-------|----------|---------------------|
| `land_relief.pmtiles` | z0–7 | land tiles | GEBCO |
|  | z8–10 | land tiles | GLO-30, GLO-90, GEBCO |
| `land_relief_eu.pmtiles` | z11–12 | EMODnet extent | GLO-30, GEBCO |

GLO-30's native pixel is about z12, so MapLibre overzooms it beyond that.

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
