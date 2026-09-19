# 06 — Land Relief

Renders shaded land relief from the Copernicus DEM, coloured on
`color-ramps/land-emodnet.txt`, a ramp sampled from the EMODnet World Base Layer.

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

The tiles carry no land mask. The viewer draws them under the vector sea mask, which
hides their sea part along the exact OSM coastline. The previous GEBCO relief rasterised
its mask on the 463 m grid, and that is what stepped over the shoreline.
